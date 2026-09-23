"""Policy: what a set, a clear, an external change or an expiry does to a lamp's record.

Pure: every function mutates the ``Record`` it is given and returns a small
result object. None of them sends anything. The caller compares ``resolve()``
before and after, and for ``expire`` reads ``ExpireResult.render``, to decide
whether a render may start (SPEC section 7.3).

SPEC section 5:

- ``apply_set``       a ``layers.set`` on one lamp: its base, its active layer, or a named layer;
- ``apply_clear``     a ``layers.clear`` on one lamp: a named layer, the active one, or all;
- ``apply_external``  a change Layers did not make, under the lamp's policy;
- ``apply_replay``    a retry loop re-applying a press made before the layers last changed;
- ``expire``          remove what ran out, and decide whether that may send anything;
- ``lift_on_off``, ``record_observed_as_base``: small helpers for the engine.

Expired layers
--------------
A layer whose ``expires_at`` has passed but which ``expire`` has not removed
yet is not live. That happens when Home Assistant was down, or while a TTL
timer is held through the startup grace. ``apply_set`` treats such an id as
absent. ``apply_clear`` does not remove it: it marks it ``on_expire: render``
and leaves it for ``expire``, which then removes it and renders like the
explicit clear it now is. It is never dropped silently while the lamp still
shows it, and an owner's clear of it is never swallowed by the safety rule.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .capability import MATCH_YES, matches, observed_to_command, project, raises_output
from .model import (
    Caps,
    Command,
    DIV_DELIVERY,
    DIV_MANUAL_KEEP,
    DIV_PARTIAL,
    External,
    GROUP_BRIGHTNESS,
    GROUP_COLOR,
    GROUP_STATE,
    LAYER_ACTIVE,
    LAYER_ALL,
    LAYER_BASE,
    Layer,
    MODES,
    MODE_ADJUST,
    MODE_FOLLOW,
    MODE_SET,
    OFF,
    OFF_COMMAND,
    ON,
    ON_EXPIRE_RENDER,
    Observed,
    POLICIES,
    POLICY_BASE_KEEP_LAYERS,
    POLICY_EDIT_ACTIVE,
    POLICY_REASSERT,
    POLICY_TAKE_BACK,
    REASSERT_COOLDOWN_S,
    Record,
    SRC_DEVICE,
    SetRequest,
    Tombstone,
    merge_command,
)
from .resolve import active_layer, editable_layer, live_layers, resolve, state_holder

# SetResult.result
SET_CREATED = "created"
SET_UPDATED = "updated"
SET_REFRESHED = "refreshed"
SET_BASE = "base_set"
SET_ACTIVE = "active_set"
SET_SKIPPED_TOMBSTONED = "skipped_tombstoned"
SET_SKIPPED_ABSENT = "skipped_absent"

# PolicyError.code. Each is also a translation key under "exceptions".
ERR_PRIORITY_REQUIRED = "priority_required"
ERR_PRIORITY_CONFLICT = "priority_conflict"
ERR_INVALID_REQUEST = "invalid_request"     # a reserved id where it makes no sense, an unknown mode

# Record.base_source values written here. External changes write their SRC_* source instead.
BASE_SRC_SERVICE = "service"
BASE_SRC_EXPIRY = "expiry"

# External.policy for a replayed foreign press (apply_replay). apply_external
# accepts it too, so a FOLLOW_UP can re-apply last_external.policy as it is.
POLICY_REPLAY = "replay"

_ATTR_GROUPS = frozenset({GROUP_BRIGHTNESS, GROUP_COLOR})


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


class PolicyError(Exception):
    """A request the model refuses.

    ``code`` is the translation key (``priority_required``,
    ``priority_conflict``, ``invalid_request``); ``placeholders`` fill its
    message (``entity_id``, ``layer``, and ``priority``/``other`` or ``reason``).
    """

    def __init__(self, code: str, **placeholders: str) -> None:
        super().__init__(code)
        self.code = code
        self.placeholders = placeholders

    def __str__(self) -> str:
        details = ", ".join(f"{k}={v}" for k, v in self.placeholders.items())
        return f"{self.code} ({details})" if details else self.code


@dataclass(frozen=True, slots=True)
class SetResult:
    """What a ``layers.set`` did to one lamp."""

    result: str                 # one of the SET_* values
    layer: str                  # the layer it resolved to: a layer id, or "base"


@dataclass(frozen=True, slots=True)
class ClearResult:
    """What a ``layers.clear`` removed from one lamp.

    ``expired`` lists layers that had already run out but that ``expire`` had
    not removed yet. They are marked ``on_expire: render`` and left for
    ``expire``, which renders their removal like this clear. A ``resolve()``
    before/after comparison cannot see them.
    """

    removed: tuple[str, ...] = ()   # layer ids, bottom of the stack first
    lifted: tuple[str, ...] = ()    # tombstone ids
    expired: tuple[str, ...] = ()   # expired layer ids, left for expire() to render


@dataclass(frozen=True, slots=True)
class ExternalResult:
    """What a policy did with an external change."""

    dropped: tuple[str, ...] = ()   # layers dropped and tombstoned, bottom of the stack first
    edited: str | None = None       # the layer edit_active changed
    partial: bool = False           # the lamp was marked diverged=partial
    reassert: bool = False          # reassert: base and layers kept, the engine re-renders


@dataclass(frozen=True, slots=True)
class ExpireResult:
    """What ran out, and whether the new effective command may be sent."""

    expired: tuple[str, ...] = ()   # layer ids, bottom of the stack first
    render: bool = False
    lifted: tuple[str, ...] = ()    # tombstone ids that ran out


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _stack_key(layer: Layer) -> tuple[int, int]:
    return (layer.priority, layer.seq)


def _set_base(rec: Record, command: Command | None, source: str, now: float) -> None:
    rec.base = command
    rec.base_source = source
    rec.base_at = now


def _priority_holder(rec: Record, priority: int, layer_id: str, now: float) -> str | None:
    """The live layer other than ``layer_id`` that holds ``priority`` on this lamp, if any."""
    for layer in live_layers(rec, now):
        if layer.priority == priority and layer.id != layer_id:
            return layer.id
    return None


def _check_priority(rec: Record, priority: int, layer_id: str, now: float) -> None:
    other = _priority_holder(rec, priority, layer_id, now)
    if other is not None:
        raise PolicyError(
            ERR_PRIORITY_CONFLICT,
            entity_id=rec.entity_id,
            layer=layer_id,
            priority=str(priority),
            other=other,
        )


def _merge_attributes(cmd: Command, over: Command, groups: frozenset[str] | None) -> Command:
    """An adjust layer's command with the brightness and colour ``over`` gives (within ``groups``).

    An adjust layer carries attributes only, so no state is merged in and its
    own state is left alone (the resolver ignores it). An ``off`` there would
    make ``merge_command`` drop every attribute, so it counts as no state.
    """
    take = _ATTR_GROUPS if groups is None else _ATTR_GROUPS & groups
    below = cmd if cmd.state != OFF else Command(None, cmd.brightness, cmd.color)
    return merge_command(below, over, take)


def _report_caps(obs: Observed) -> Caps:
    """Stand-in capabilities when the caller gave none: the mode the lamp reports."""
    return Caps(frozenset({obs.color_mode}) if obs.color_mode else frozenset())


def _shows(rec: Record, cmd: Command | None, caps: Caps | None) -> bool | None:
    """Does the lamp's latest report show ``cmd``, as the lamp would take it?

    ``None`` when that is not known: no report, the lamp is away, or nothing to show.
    """
    obs = rec.observed
    if obs is None or not obs.available or cmd is None:
        return None
    lamp = caps if caps is not None else _report_caps(obs)
    return matches(obs, project(cmd, lamp), lamp) == MATCH_YES


def _partial(rec: Record, groups: frozenset[str] | None, now: float, caps: Caps | None) -> bool:
    """A change that took only ``groups`` left the lamp showing something else in the rest (SPEC 5.3).

    "The rest" is the new effective command's brightness and colour that the
    change did not take. On the state path they are what the lamp showed, e.g.
    a layer's colour under a brightness-only call; on the intent path the call
    only named some groups. A lamp that shows the rest anyway is not partial,
    and neither is one whose report is unknown.
    """
    if groups is None:
        return False
    after = resolve(rec, now).command
    if after is None or not after.is_on:
        return False
    rest = Command(
        ON,
        None if GROUP_BRIGHTNESS in groups else after.brightness,
        None if GROUP_COLOR in groups else after.color,
    )
    return _shows(rec, rest, caps) is False


def _drop_all(rec: Record, source: str, now: float) -> tuple[str, ...]:
    """Drop every layer on the lamp but its follow layers, and tombstone each one (take_back).

    A follow layer is never dropped by a change Layers did not make: the change
    takes over only the groups it chose (``_take_over``).
    """
    dropped = sorted((lay for lay in rec.layers.values() if lay.mode != MODE_FOLLOW), key=_stack_key)
    for layer in dropped:
        rec.tombstones[layer.id] = Tombstone(
            layer_id=layer.id,
            at=now,
            expires_at=layer.expires_at,
            lift_when_off=layer.resume_after_manual,
            source=source,
        )
    for layer in dropped:
        del rec.layers[layer.id]
    return tuple(layer.id for layer in dropped)


def _take_over(rec: Record, chosen: frozenset[str], now: float) -> None:
    """A person chose ``chosen`` (brightness and/or colour): each follow layer stops
    following those until the lamp is seen off, or its ``manual_timeout`` runs out."""
    chosen = chosen & _ATTR_GROUPS
    if not chosen:
        return
    for layer in rec.layers.values():
        if layer.mode != MODE_FOLLOW or not layer.live(now):
            continue
        layer.manual = layer.manual | chosen
        if layer.manual_timeout is not None:
            layer.manual_until = now + layer.manual_timeout


def _renew(layer: Layer, req: SetRequest, *, keep_lease: bool = False) -> None:
    """What a refresh changes: the lease, and the owner when one is given.

    ``keep_lease``: a request without an expiry leaves the one the layer has. An
    identical set only extends the time; it never silently makes a leased layer
    permanent. An update (a different command) replaces the layer, expiry included.
    """
    if not (keep_lease and req.expires_at is None):
        layer.expires_at = req.expires_at
    if req.owner is not None:
        layer.owner = req.owner


def _update_options(layer: Layer, req: SetRequest) -> None:
    """What an update changes besides the command: the lease, the owner if given, the options."""
    _renew(layer, req)
    layer.resume_after_manual = req.resume_after_manual
    layer.on_expire = req.on_expire


def _requested_mode(layer: Layer) -> str:
    """The mode the owner asked for (an edit can turn an adjust layer into set/off)."""
    return layer.requested_mode or layer.mode


def _as_mode(cmd: Command, mode: str) -> Command:
    """A request's command as a layer of ``mode`` reads it.

    A ``set`` layer given attributes and no state means "on like this" (as when
    it was created); an ``adjust`` layer carries attributes only, its "on"
    comes from below.
    """
    if mode == MODE_SET and cmd.state is None:
        return Command(ON, cmd.brightness, cmd.color)
    if mode == MODE_ADJUST and cmd.state == ON:
        return Command(None, cmd.brightness, cmd.color)
    return cmd


def _to_set_off(layer: Layer) -> None:
    """An edit turning an adjust layer off: it becomes set/off, remembering what was asked."""
    if layer.requested_mode is None:
        layer.requested_mode = layer.mode
    layer.mode = MODE_SET
    layer.command = OFF_COMMAND


# --------------------------------------------------------------------------- #
# layers.set
# --------------------------------------------------------------------------- #


def apply_set(
    rec: Record, req: SetRequest, now: float, next_seq: Callable[[], int]
) -> SetResult:
    """Apply one ``layers.set`` to one lamp (SPEC 5.1).

    - ``base``: merged into the base; an unknown base given no state turns on.
    - ``active``: edits the layer ``active_layer()`` names (``command``, never
      ``requested``); an ``adjust`` one given ``state: off`` becomes ``set``/off.
      With no active layer it is a ``base`` set.
    - a named id: skipped while tombstoned; otherwise created, updated, or, when
      the request is what the owner already asked for (command, the mode it
      asked for, priority), only refreshed: a new lease and the owner if
      given, nothing else; no re-stacking, and edits to ``command`` (an
      adjust layer turned off included) survive. ``only_if_present`` never
      creates and never changes a layer's mode.

    Raises ``PolicyError`` before changing anything: ``invalid_request`` for a
    command that sets nothing, a named layer whose ``expires_at`` is not after
    ``now``, a reserved id or an unknown mode. Every result but ``refreshed``
    and ``skipped_*`` sets ``last_layers_change``.
    """
    if req.mode == MODE_FOLLOW:
        _check_follow(rec, req)
    elif not req.command.groups() and not _renews_follow(rec, req, now):
        raise PolicyError(
            ERR_INVALID_REQUEST,
            entity_id=rec.entity_id,
            layer=req.layer,
            reason="the request sets no state, brightness or colour",
        )
    if req.layer == LAYER_BASE:
        return _set_base_layer(rec, req, now)
    if req.layer == LAYER_ACTIVE:
        return _set_active_layer(rec, req, now)
    if req.layer == LAYER_ALL:
        raise PolicyError(
            ERR_INVALID_REQUEST,
            entity_id=rec.entity_id,
            layer=req.layer,
            reason="'all' is not a layer id",
        )
    return _set_named_layer(rec, req, now, next_seq)


def _set_base_layer(rec: Record, req: SetRequest, now: float) -> SetResult:
    merged = merge_command(rec.base, req.command)
    if merged.state is None:        # the base state was unknown and the command gives none
        merged = Command(ON, merged.brightness, merged.color)
    _set_base(rec, merged, BASE_SRC_SERVICE, now)
    rec.last_layers_change = now
    return SetResult(SET_BASE, LAYER_BASE)


def _set_active_layer(rec: Record, req: SetRequest, now: float) -> SetResult:
    top = editable_layer(rec, now)
    if top is None:
        return _set_base_layer(rec, req, now)
    if top.mode == MODE_ADJUST:
        if req.command.state == OFF:
            _to_set_off(top)
        else:
            top.command = _merge_attributes(top.command, req.command, None)
    else:
        top.command = merge_command(top.command, req.command)
    rec.last_layers_change = now
    return SetResult(SET_ACTIVE, top.id)


def _check_follow(rec: Record, req: SetRequest) -> None:
    """A follow request names a source and nothing a source would give."""
    reason = None
    if req.layer in (LAYER_BASE, LAYER_ACTIVE):
        reason = "a follow layer needs a layer id"
    elif not req.source:
        # A renewal may leave the source out: it keeps the one the layer follows.
        layer = rec.layers.get(req.layer)
        if not req.only_if_present:
            reason = "a follow layer needs a source"
        elif layer is not None and _requested_mode(layer) != MODE_FOLLOW:
            reason = "a renewal cannot turn a layer into a follow layer"
    elif req.source == rec.entity_id:
        reason = "a lamp cannot follow itself"
    elif req.command.groups():
        reason = "a follow layer takes its state, brightness and colour from its source"
    if reason is not None:
        raise PolicyError(ERR_INVALID_REQUEST, entity_id=rec.entity_id, layer=req.layer,
                          reason=reason)


def _renews_follow(rec: Record, req: SetRequest, now: float) -> bool:
    """A renewal (``only_if_present``) of a follow layer carries no command."""
    layer = rec.layers.get(req.layer)
    return (req.only_if_present and layer is not None and layer.live(now)
            and _requested_mode(layer) == MODE_FOLLOW)


def _follow_options(layer: Layer, req: SetRequest) -> None:
    """A follow request's own options; one a request leaves out keeps its value."""
    if req.manual_timeout is not None:
        layer.manual_timeout = req.manual_timeout
    if req.transition is not None:
        layer.transition = req.transition


def _set_named_layer(
    rec: Record, req: SetRequest, now: float, next_seq: Callable[[], int]
) -> SetResult:
    layer_id = req.layer
    if req.mode not in MODES:
        raise PolicyError(
            ERR_INVALID_REQUEST,
            entity_id=rec.entity_id,
            layer=layer_id,
            reason=f"unknown mode {req.mode!r}",
        )
    if req.expires_at is not None and req.expires_at <= now:
        # Refreshing or creating a layer that is already over would change the
        # effective command behind expire()'s safety rule.
        raise PolicyError(
            ERR_INVALID_REQUEST,
            entity_id=rec.entity_id,
            layer=layer_id,
            reason="expires_at is not in the future",
        )
    tombstone = rec.tombstones.get(layer_id)
    if tombstone is not None and tombstone.live(now):
        return SetResult(SET_SKIPPED_TOMBSTONED, layer_id)

    layer = rec.layers.get(layer_id)
    if layer is None or not layer.live(now):
        if req.only_if_present:
            return SetResult(SET_SKIPPED_ABSENT, layer_id)
        if req.priority is None:
            raise PolicyError(ERR_PRIORITY_REQUIRED, entity_id=rec.entity_id, layer=layer_id)
        _check_priority(rec, req.priority, layer_id, now)
        rec.layers[layer_id] = Layer(
            id=layer_id,
            priority=req.priority,
            mode=req.mode,
            requested=req.command,
            command=req.command,
            seq=next_seq(),
            set_at=now,
            expires_at=req.expires_at,
            owner=req.owner,
            resume_after_manual=req.resume_after_manual,
            on_expire=req.on_expire,
            source=req.source if req.mode == MODE_FOLLOW else None,
            manual_timeout=req.manual_timeout if req.mode == MODE_FOLLOW else None,
            transition=req.transition if req.mode == MODE_FOLLOW else None,
        )
        rec.last_layers_change = now
        return SetResult(SET_CREATED, layer_id)

    if _requested_mode(layer) == MODE_FOLLOW and (req.only_if_present or req.mode == MODE_FOLLOW):
        return _set_follow_layer(rec, layer, req, now)

    asked = _requested_mode(layer)
    # A renewal (only_if_present) never changes a layer's mode: it must not turn an
    # adjust layer into a set layer that lights the lamp. Its command is read the
    # way the layer's mode reads one, so leaving the mode out still only refreshes.
    mode = asked if req.only_if_present else req.mode
    command = _as_mode(req.command, mode) if req.only_if_present else req.command
    new_priority = req.priority is not None and req.priority != layer.priority
    if command == layer.requested and mode == asked and not new_priority:
        _renew(layer, req, keep_lease=True)
        return SetResult(SET_REFRESHED, layer_id)

    if new_priority:
        _check_priority(rec, req.priority, layer_id, now)
        layer.priority = req.priority
    layer.mode = mode
    layer.requested_mode = None
    layer.requested = command
    layer.command = command
    layer.set_at = now
    if mode == MODE_FOLLOW:     # a set/adjust layer becomes a follow layer
        layer.source = req.source
        layer.manual_timeout = req.manual_timeout
        layer.transition = req.transition
    else:
        layer.source = layer.manual_timeout = layer.transition = None
    layer.manual = frozenset()
    layer.manual_until = None
    _update_options(layer, req)
    rec.last_layers_change = now
    return SetResult(SET_UPDATED, layer_id)


def _set_follow_layer(rec: Record, layer: Layer, req: SetRequest, now: float) -> SetResult:
    """A set of an existing follow layer: the same source (or a renewal) only refreshes.

    Its command is the engine's copy of the source, so no request compares
    against it. A new source or priority is an update: what a person took over
    from the old source does not carry over.
    """
    new_priority = req.priority is not None and req.priority != layer.priority
    new_source = not req.only_if_present and req.source != layer.source
    if not new_priority and not new_source:
        _renew(layer, req, keep_lease=True)
        _follow_options(layer, req)
        return SetResult(SET_REFRESHED, layer.id)
    if new_priority:
        _check_priority(rec, req.priority, layer.id, now)
        layer.priority = req.priority
    if new_source:
        layer.source = req.source
        layer.command = Command(None)
    layer.manual = frozenset()
    layer.manual_until = None
    layer.set_at = now
    _update_options(layer, req)
    _follow_options(layer, req)
    rec.last_layers_change = now
    return SetResult(SET_UPDATED, layer.id)


# --------------------------------------------------------------------------- #
# layers.clear
# --------------------------------------------------------------------------- #


def apply_clear(rec: Record, layer: str, now: float) -> ClearResult:
    """Apply one ``layers.clear`` to one lamp (SPEC 5.2).

    - a layer id: removes that layer and that id's tombstone;
    - ``active``: removes the layer ``active_layer()`` names, if any;
    - ``all``: removes every layer and every tombstone.

    Live layers are removed. An expired one that ``expire`` has not removed yet
    (Home Assistant was down, or the startup grace holds its timer) is marked
    ``on_expire: render`` and left for ``expire``, which removes it and renders
    like this clear: the owner's clear is a restore, and the safety rule must
    not swallow it (``ClearResult.expired``). Sets ``last_layers_change`` when
    a layer was removed or marked; lifting a tombstone alone changes no layer.
    """
    if layer == LAYER_BASE:
        raise PolicyError(
            ERR_INVALID_REQUEST,
            entity_id=rec.entity_id,
            layer=layer,
            reason="the base cannot be cleared",
        )
    expired: tuple[str, ...] = ()
    if layer == LAYER_ACTIVE:
        top = active_layer(rec, now)
        removed = (top.id,) if top is not None else ()
        lifted: tuple[str, ...] = ()
    elif layer == LAYER_ALL:
        removed = tuple(lay.id for lay in live_layers(rec, now))
        over = sorted((lay for lay in rec.layers.values() if not lay.live(now)), key=_stack_key)
        expired = tuple(lay.id for lay in over)
        lifted = tuple(sorted(rec.tombstones))
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
    return ClearResult(removed, lifted, expired)


# --------------------------------------------------------------------------- #
# External changes
# --------------------------------------------------------------------------- #


def apply_external(
    rec: Record,
    shown: Command,
    groups: frozenset[str] | None,
    source: str,
    policy: str,
    now: float,
    user_id: str | None = None,
    *,
    caps: Caps | None = None,
    chosen: frozenset[str] | None = None,
) -> ExternalResult:
    """Record a change Layers did not make, under the lamp's policy (SPEC 5.3).

    ``shown`` is what the lamp now shows, or the intent of a known call;
    ``groups`` is ``None`` (take every group ``shown`` gives) or the groups a
    known call specified. What the lamp shows is read from ``rec.observed``,
    which the caller updates first; ``caps`` are the lamp's (without them the
    reported colour mode stands in).

    - ``take_back``: the change is merged into the base, and every layer is
      dropped and tombstoned. The tombstone inherits the layer's ``expires_at``,
      and ``lift_when_off`` from its ``resume_after_manual``. ``diverged`` becomes
      ``partial`` when the lamp does not show the new effective command in the
      groups the change did not take, else ``None``.
    - ``edit_active``: the change is merged into the active layer's ``command``
      (``requested`` is kept); an ``adjust`` one shown off becomes ``set``/off.
      An on/off decision also reaches the base, so clearing the layer never
      undoes it: an off becomes the base, and an on over a base that is off (or
      unknown) is merged into it. ``diverged`` is left as it is. With no active
      layer it is a ``take_back``.
    - ``base_keep_layers``: the change is merged into the base and the layers
      stay. If a layer is active and the lamp does not show the effective
      command, it is marked ``manual_keep``; otherwise ``diverged`` follows the
      ``take_back`` rule.
    - ``reassert``: a ``device`` change (no service call behind it) while a
      layer is active is the device misbehaving: the base and the layers stay,
      ``diverged = delivery`` and the result says ``reassert`` so the engine
      sends the effective command again. A ``user`` or ``automation`` change, a
      change with no active layer, or a second ``device`` change within
      ``REASSERT_COOLDOWN_S`` of a reassert is a ``take_back``.
    - ``replay`` (``POLICY_REPLAY``): see ``apply_replay``.

    Follow layers are never dropped or edited. Under every policy but a
    reassert, each one stops following the brightness/colour the person
    ``chosen`` (default: the attribute groups the change took); the caller
    passes what was really chosen: a known call's named groups, nothing for a
    lamp that just came on at its power-on level.

    Every policy but ``replay`` clears ``owed``; all record ``last_external``.
    None of them touches ``last_layers_change``, which marks changes made
    through Layers.
    """
    if policy == POLICY_REPLAY:
        return apply_replay(rec, shown, groups, source, now, user_id)
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}")
    dropped: tuple[str, ...] = ()
    edited: str | None = None
    partial = False
    reassert = False

    if policy == POLICY_REASSERT:
        last = rec.last_external
        recent = (last is not None and last.policy == POLICY_REASSERT
                  and now - last.at < REASSERT_COOLDOWN_S)
        if source == SRC_DEVICE and editable_layer(rec, now) is not None and not recent:
            reassert = True
        else:
            policy = POLICY_TAKE_BACK

    if not reassert:
        taken = shown.groups() if groups is None else shown.groups() & groups
        _take_over(rec, taken if chosen is None else chosen, now)

    top = editable_layer(rec, now) if policy == POLICY_EDIT_ACTIVE else None
    if reassert:
        rec.diverged = DIV_DELIVERY
    elif top is not None:
        _edit_from_external(top, shown, groups)
        edited = top.id
        taken = shown.groups() if groups is None else shown.groups() & groups
        if GROUP_STATE in taken:
            # An off must survive the layer: a house-wide Off while a layer holds
            # the lamp would otherwise relight it when the owner clears the layer.
            # An on over a base that is off would turn it off again then.
            if shown.state == OFF:
                _set_base(rec, OFF_COMMAND, source, now)
            elif rec.base is None or rec.base.state != ON:
                _set_base(rec, merge_command(rec.base, shown, groups), source, now)
    elif policy == POLICY_BASE_KEEP_LAYERS:
        _set_base(rec, merge_command(rec.base, shown, groups), source, now)
        kept_on_show = (
            editable_layer(rec, now) is not None
            and _shows(rec, resolve(rec, now).command, caps) is False
        )
        if kept_on_show:
            rec.diverged = DIV_MANUAL_KEEP
        else:
            partial = _partial(rec, groups, now, caps)
            rec.diverged = DIV_PARTIAL if partial else None
    else:   # take_back, or edit_active with no active layer
        _set_base(rec, merge_command(rec.base, shown, groups), source, now)
        dropped = _drop_all(rec, source, now)
        partial = _partial(rec, groups, now, caps)
        rec.diverged = DIV_PARTIAL if partial else None

    rec.owed = None
    rec.last_external = External(
        at=now, source=source, policy=POLICY_REASSERT if reassert else policy, user_id=user_id,
        dropped=dropped, edited=edited, groups=groups,
    )
    return ExternalResult(dropped, edited, partial, reassert)


def _edit_from_external(top: Layer, shown: Command, groups: frozenset[str] | None) -> None:
    """edit_active: put what the lamp shows into the active layer's command."""
    if top.mode == MODE_ADJUST:
        taken = shown.groups() if groups is None else shown.groups() & groups
        if GROUP_STATE in taken and shown.state == OFF:
            _to_set_off(top)
        else:
            top.command = _merge_attributes(top.command, shown, groups)
    else:
        top.command = merge_command(top.command, shown, groups)


def apply_replay(
    rec: Record,
    shown: Command,
    groups: frozenset[str] | None,
    source: str,
    now: float,
    user_id: str | None = None,
) -> ExternalResult:
    """A retry loop re-applying a press made before the lamp's layers last changed (SPEC 5.3).

    The press is old news for the layers: it is merged into the base (the
    lamp's own state underneath), and the layers, tombstones, ``diverged`` and
    ``owed`` stay as they are. ``last_external`` records it with policy
    ``replay``, so a follow-up re-applies it the same way. The engine re-renders
    ``REPLAY_QUIET_S`` after the last replayed call (SPEC 7.3 f).
    """
    _set_base(rec, merge_command(rec.base, shown, groups), source, now)
    rec.last_external = External(at=now, source=source, policy=POLICY_REPLAY, user_id=user_id,
                                 groups=groups)
    return ExternalResult()


# --------------------------------------------------------------------------- #
# Expiry
# --------------------------------------------------------------------------- #


def expire(rec: Record, now: float, observed: Observed | None, caps: Caps) -> ExpireResult:
    """Remove the layers and tombstones whose ``expires_at <= now`` (SPEC 5.4).

    When layers expired:

    - a live ``set`` layer still decides on/off afterwards: ``render`` (an owner
      still holds the lamp). An ``adjust`` layer does not count: its "on" is
      the base's, and an expiry never lights a lamp through it;
    - else, if one of them had ``on_expire: render`` (or was cleared after it
      ran out): ``render``;
    - else the safety rule. If going from what the lamp shows (``observed``, or
      ``p_at_drop`` when it is unavailable) to the new effective command would
      light or brighten it, nothing is sent and the base becomes what the lamp
      shows (``base_source = "expiry"``). Otherwise ``render``.

    ``render`` is never true when the effective command is ``None``: there is
    nothing to send. Sets ``last_layers_change`` when a layer expired;
    tombstones running out on their own do not.
    """
    lifted = tuple(sorted(tid for tid, tomb in rec.tombstones.items() if not tomb.live(now)))
    for layer_id in lifted:
        del rec.tombstones[layer_id]

    gone = sorted((lay for lay in rec.layers.values() if not lay.live(now)), key=_stack_key)
    if not gone:
        return ExpireResult((), False, lifted)
    for layer in gone:
        del rec.layers[layer.id]
    rec.last_layers_change = now
    expired = tuple(layer.id for layer in gone)

    effective = resolve(rec, now).command
    if state_holder(rec, now) is not None:
        render = True
    elif any(layer.on_expire == ON_EXPIRE_RENDER for layer in gone):
        render = True
    else:
        shown = observed if observed is not None and observed.available else rec.p_at_drop
        if raises_output(shown, effective):
            _set_base(rec, observed_to_command(shown, caps), BASE_SRC_EXPIRY, now)
            render = False
        else:
            render = True
    return ExpireResult(expired, render and effective is not None, lifted)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def lift_on_off(rec: Record) -> tuple[str, ...]:
    """Remove the tombstones that lift when the lamp is seen off; return their ids."""
    lifted = tuple(sorted(tid for tid, tomb in rec.tombstones.items() if tomb.lift_when_off))
    for layer_id in lifted:
        del rec.tombstones[layer_id]
    return lifted


def release_manual(rec: Record, now: float, *, off: bool = False) -> tuple[str, ...]:
    """Follow layers follow again what a person took over: all of them when the lamp
    is seen off (``off``), else those whose ``manual_until`` has passed. Returns
    the ids of the layers that changed."""
    released = []
    for layer in sorted(rec.layers.values(), key=_stack_key):
        if layer.mode != MODE_FOLLOW or not layer.manual:
            continue
        if off or (layer.manual_until is not None and layer.manual_until <= now):
            layer.manual = frozenset()
            layer.manual_until = None
            released.append(layer.id)
    return tuple(released)


def record_observed_as_base(
    rec: Record, obs: Observed | None, caps: Caps, now: float, source: str
) -> Command | None:
    """Make what the lamp reports its base; return the new base.

    An unavailable or missing report records an unknown base (``None``).
    """
    _set_base(rec, observed_to_command(obs, caps), source, now)
    return rec.base
