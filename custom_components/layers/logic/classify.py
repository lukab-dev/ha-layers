"""Classifier: what a change on an enrolled lamp means (SPEC section 6).

Pure functions over a ``Record``, the incoming event, and a ``Runtime``
snapshot the engine builds for each call. Nothing here keeps state or mutates
the record; the engine acts on the verdict.

- ``classify_state`` judges a ``state_changed`` on an enrolled lamp;
- ``settle_debounce`` judges a no-context change once it has held ``DEBOUNCE_S``;
- ``classify_call`` is the intent path, for foreign calls that may produce no
  ``state_changed`` at all (the lamp already shows what was asked, or is away);
- ``decide_return`` judges a lamp ``RETURN_SETTLE_S`` after it came back.

Context rules, from Home Assistant's behaviour:

- a state written within 5 s of a service call carries that call's context,
  whoever wrote it, so a context of ours only counts when the state agrees
  with our command, and a context of ours never counts as someone else's;
- later writes, Hue-room commands and HomeKit carry a context with neither
  ``user_id`` nor ``parent_id``, which says nothing about who acted;
- a write that carries our context but not our command is someone else's
  inside those 5 s. The measured bridge reversals come later (9.7-38 s), so
  such a write is never a failed delivery of ours.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .capability import (
    ATTR_BRIGHTNESS,
    MATCH_NO,
    MATCH_YES,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    close,
    compared_groups,
    matches,
    project,
    raises_output,
)
from .model import (
    ARRIVED_HOLD_S,
    COLOR_KELVIN,
    COLOR_XY,
    Call,
    Caps,
    Command,
    DIV_MANUAL_KEEP,
    DIV_UNSYNCED,
    FOLLOW_UP_S,
    GROUP_BRIGHTNESS,
    GROUP_COLOR,
    GROUP_STATE,
    LATE_WINDOW_DEFAULT_S,
    LATE_WINDOW_S,
    OFF,
    OFF_COMMAND,
    ON,
    OWED_ON_MAX_AGE_S,
    Observed,
    Record,
    SRC_AUTOMATION,
    SRC_DEVICE,
    SRC_OURS,
    SRC_USER,
    TOL_BRIGHTNESS,
    TOL_KELVIN,
    TOL_XY,
)
from .resolve import active_layer, resolve, state_holder

SERVICE_TOGGLE = "toggle"

# Verdict kinds, in the order classify_state tests them.
GONE = "gone"                           # the entity was removed
TRANSPORT_DOWN = "transport_down"       # the lamp went unavailable/unknown
IGNORE = "ignore"                       # nothing to judge (still away, or away again)
FIRST = "first"                         # first report since startup: update observed only
TRANSPORT_UP = "transport_up"           # back from unavailable/unknown; decide_return judges it
OURS = "ours"                           # our own command landing
EXTERNAL = "external"                   # a change Layers did not make
FAILED_DELIVERY = "failed_delivery"     # a late no-context reversal of the last command
NOISE = "noise"                         # no context while our render runs or a return settles
FOLLOW_UP = "follow_up"                 # an attribute tail of a recent external change
DEBOUNCE = "debounce"                   # no context: judge with settle_debounce after DEBOUNCE_S
REREPORT = "rereport"                   # settle_debounce: nothing really changed
EXTERNAL_INTENT = "external_intent"     # classify_call: a foreign call's intent, from its data

# ReturnDecision kinds (EXTERNAL is shared with the verdicts).
RECORD = "record"       # base := what the lamp shows; nothing sent
NOTHING = "nothing"     # it already shows its effective command
SEND = "send"           # deliver the effective command
WAIT = "wait"           # not back after all: keep owed, the next return decides


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StateEvent:
    """A ``state_changed`` on an enrolled lamp, reduced to what the classifier reads."""

    entity_id: str
    old: Observed | None
    new: Observed | None
    context_id: str | None
    parent_id: str | None
    user_id: str | None
    at: float


@dataclass(frozen=True, slots=True)
class CallInfo:
    """A foreign ``light.*`` service call, normalised by the engine."""

    context_id: str
    source: str                     # "user" if user_id else "automation"
    user_id: str | None
    service: str                    # "turn_on" | "turn_off" | "toggle"
    command: Command | None         # the intent (None for toggle, or an intent it cannot know)
    groups: frozenset[str]          # attribute groups the call specified
    lamps: frozenset[str]           # enrolled lamps it targets (groups/rooms expanded)
    via_room: frozenset[str]        # the subset reached through a vendor room/zone group
    first_seen: float               # when this context id was FIRST seen, kept across re-sends


@dataclass(frozen=True, slots=True)
class OurCommand:
    """A command of ours in flight on a lamp, under one context id."""

    context_id: str
    target: Command
    at: float


@dataclass(frozen=True, slots=True)
class Runtime:
    """What the engine knows right now that is not in the record."""

    ours: Mapping[str, OurCommand] = field(default_factory=dict)   # per context id, this lamp
    render_alive: bool = False                                      # a render of ours runs on it
    calls: Mapping[str, CallInfo] = field(default_factory=dict)    # foreign calls by context id
    room_call: CallInfo | None = None                               # a recent room call reaching it
    returning: bool = False     # it came back and decide_return has not judged it yet


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Verdict:
    """What a change means, and what the engine needs to act on it.

    ``groups`` is ``None`` when every group the lamp shows is taken. ``ours``
    is set on ``FAILED_DELIVERY`` (was the reverted command ours?), and
    ``command`` carries the command to act on: the intent of an
    ``EXTERNAL_INTENT``, the reverted target of a ``FAILED_DELIVERY``.
    """

    kind: str
    source: str | None = None
    call: CallInfo | None = None
    groups: frozenset[str] | None = None
    replay: bool = False
    ours: bool | None = None
    command: Command | None = None


@dataclass(frozen=True, slots=True)
class ReturnDecision:
    """What to do with a lamp that came back (``EXTERNAL`` carries ``source=device``)."""

    kind: str
    source: str | None = None

    @property
    def clears_owed(self) -> bool:
        """``SEND`` renders (which owes afresh) and ``WAIT`` keeps waiting; the rest settle it."""
        return self.kind in (RECORD, NOTHING, EXTERNAL)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _on_off(obs: Observed | None) -> str | None:
    """``on``/``off``, or ``None`` for anything else."""
    return obs.state if obs is not None and obs.state in (ON, OFF) else None


def _flipped(old: Observed | None, new: Observed | None) -> bool:
    """The report went on -> off or off -> on."""
    a, b = _on_off(old), _on_off(new)
    return a is not None and b is not None and a != b


def _reaches(call: CallInfo, entity_id: str) -> bool:
    return entity_id in call.lamps or entity_id in call.via_room


def _replay(rec: Record, call: CallInfo) -> bool:
    """A retry loop re-applying a press made before the lamp's layers last changed."""
    return rec.last_layers_change is not None and call.first_seen < rec.last_layers_change


def _call_groups(rec: Record, call: CallInfo) -> frozenset[str] | None:
    """The groups a call's state change should take: all shown (``None``) unless the call says.

    A toggle, an intent Layers cannot know (``command`` is ``None``: a brightness
    step, a profile), or a call that does not name this lamp takes everything
    the lamp shows. A ``turn_on``/``turn_off`` always decides the state as well.
    """
    if (
        call.command is None
        or call.service == SERVICE_TOGGLE
        or not call.groups
        or not _reaches(call, rec.entity_id)
    ):
        return None
    return _with_state(call)


def _with_state(call: CallInfo) -> frozenset[str]:
    """A call's groups, plus ``state`` for a ``turn_on``/``turn_off``, which decides it."""
    if call.service in (SERVICE_TURN_ON, SERVICE_TURN_OFF):
        return frozenset(call.groups | {GROUP_STATE})
    return frozenset(call.groups)


def _external_from_call(rec: Record, call: CallInfo, flipped: bool = False) -> Verdict:
    """``flipped``: the lamp went off -> on or on -> off. A bare ``turn_on`` names only the
    state, but the lamp coming on decided its brightness and colour too (as the device
    path already treats a flip): taking ``{state}`` alone would leave a base that is on
    with nothing else, and never learns what the lamp shows."""
    groups = None if flipped else _call_groups(rec, call)
    return Verdict(EXTERNAL, source=call.source, call=call, groups=groups,
                   replay=_replay(rec, call))


def _xy_distance(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def _further(old: Observed, new: Observed, call: Call | None) -> bool:
    """Did ``new`` get further from ``call`` than ``old`` was, by more than tolerance?

    Only values the call carries and both reports carry are compared, so a
    step towards the target, or one that cannot be told, never counts.
    """
    if call is None:
        return False
    data = dict(call.data)
    brightness = data.get(ATTR_BRIGHTNESS)
    if brightness is not None and new.brightness is not None and old.brightness is not None:
        if abs(new.brightness - brightness) - abs(old.brightness - brightness) > TOL_BRIGHTNESS:
            return True
    kelvin = data.get(COLOR_KELVIN)
    if kelvin is not None and new.kelvin is not None and old.kelvin is not None:
        if abs(new.kelvin - kelvin) - abs(old.kelvin - kelvin) > TOL_KELVIN:
            return True
    xy = data.get(COLOR_XY)
    if xy is not None and new.xy is not None and old.xy is not None:
        if _xy_distance(new.xy, xy) - _xy_distance(old.xy, xy) > TOL_XY:
            return True
    return False


def _consistent(old: Observed, new: Observed, target: Command, caps: Caps) -> bool:
    """Could ``new`` be our ``target`` landing, or a step towards it during a transition?

    A step keeps the target's on/off state and gets no further from it. A
    person turning a lamp up inside Home Assistant's 5 s context reuse, or a
    dimming step while our target is off, is not ours.
    """
    call = project(target, caps)
    if matches(new, call, caps) != MATCH_NO:
        return True
    if _on_off(new) is None or new.state != target.state:
        return False
    return not _further(old, new, call)


def shows_target(new: Observed | None, target: Command | None, caps: Caps) -> bool:
    """Does this report show the lamp at ``target``? (What keeps ``LastCommand.matched_at``.)

    A lamp that clamps the colour it was sent (``colour_off``) has arrived too:
    verification accepts it, and it will never match any closer.
    """
    if new is None or target is None or target.state not in (ON, OFF):
        return False
    return matches(new, project(target, caps), caps) != MATCH_NO


def _moved_away(old: Observed, new: Observed, target: Command | None, caps: Caps) -> bool:
    """``new`` kept ``target``'s on/off but got further from it than ``old`` was (no flip)."""
    if target is None or target.state not in (ON, OFF) or _flipped(old, new):
        return False
    if _on_off(new) != target.state:
        return False
    return _further(old, new, project(target, caps))


def _stale_ours(rec: Record, context_id: str) -> bool:
    """Our last command's context, already out of ``Runtime.ours`` (HA reuses it for 5 s)."""
    last = rec.last_command
    return last is not None and last.ours and last.context_id == context_id


def _late(rec: Record, old: Observed, new: Observed, caps: Caps, now: float) -> bool:
    """A no-context reversal of the last command to the lamp, inside its late window.

    Only flips count, and only a flip back to where the lamp was when the
    command was sent (``from_state``, when known): the measured bridge
    reversals are exactly that. A brightness or colour move is a person's (a
    dimmer bound to the bridge reports with no context) and is never retried;
    so is turning off a lamp that a command only dimmed.
    """
    last = rec.last_command
    if last is None or last.target is None or last.target.state not in (ON, OFF):
        return False
    if now - last.at > LATE_WINDOW_S.get(caps.platform, LATE_WINDOW_DEFAULT_S):
        return False
    if not (_flipped(old, new) and new.state != last.target.state):
        return False
    return last.from_state not in (ON, OFF) or new.state == last.from_state


def _contradicts(call: CallInfo, new: Observed) -> bool:
    """The report's on/off is the opposite of what the call asked for."""
    wanted = call.command.state if call.command is not None else None
    return wanted in (ON, OFF) and _on_off(new) is not None and new.state != wanted


def _follow_up(rec: Record, old: Observed, new: Observed, now: float) -> bool:
    """An attribute tail of the last external change: nothing of ours came since.

    A layer set or a render of ours after that change makes a later report
    ours to judge (or a new change), never a tail of the old one.
    """
    ext = rec.last_external
    if ext is None or now - ext.at > FOLLOW_UP_S or _flipped(old, new):
        return False
    if rec.last_layers_change is not None and rec.last_layers_change > ext.at:
        return False
    last = rec.last_command
    return not (last is not None and last.ours and last.at > ext.at)


# --------------------------------------------------------------------------- #
# 6.1 State changes
# --------------------------------------------------------------------------- #


def classify_state(rec: Record, ev: StateEvent, rt: Runtime, caps: Caps, now: float) -> Verdict:
    """What a ``state_changed`` on an enrolled lamp means (SPEC 6.1), first match wins.

    GONE, TRANSPORT_DOWN / IGNORE, FIRST, TRANSPORT_UP, OURS, EXTERNAL (known
    call, then ``user_id``/``parent_id``), then without a usable context:
    EXTERNAL (a room call whose intent the report does not contradict), NOISE
    (a return settling, or our own render running), FAILED_DELIVERY (a late
    reversal: of ours, or of someone else's command when re-sending it would
    not light the lamp), NOISE (any render of ours), FOLLOW_UP (nothing of
    ours since the external change), DEBOUNCE. Windows are measured from ``now``.
    """
    old, new = ev.old, ev.new
    if new is None:
        return Verdict(GONE)
    if not new.available:
        if old is not None and not old.available:
            return Verdict(IGNORE)
        return Verdict(TRANSPORT_DOWN)
    if old is None:
        return Verdict(FIRST)
    if not old.available:
        return Verdict(TRANSPORT_UP)

    context_id, parent_id, user_id = ev.context_id, ev.parent_id, ev.user_id
    reused = False      # our context on someone else's write, inside HA's 5 s reuse
    if context_id is not None:
        mine = rt.ours.get(context_id)
        if mine is not None and _consistent(old, new, mine.target, caps):
            return Verdict(OURS, source=SRC_OURS)
        if mine is not None or _stale_ours(rec, context_id):
            # Judge it as if it had no context, but it is too early to be a
            # bridge reversal: it never counts as a failed delivery.
            reused = True
            context_id = parent_id = user_id = None

    if context_id is not None and context_id in rt.calls:
        return _external_from_call(rec, rt.calls[context_id], _flipped(old, new))
    if user_id or parent_id:
        return Verdict(EXTERNAL, source=SRC_USER if user_id else SRC_AUTOMATION)

    room = rt.room_call
    if room is not None and _reaches(room, rec.entity_id) and not _contradicts(room, new):
        return _external_from_call(rec, room, _flipped(old, new))
    if rt.returning:
        return Verdict(NOISE)       # decide_return judges the lamp once it has settled
    last = rec.last_command
    if rt.render_alive and (last is None or last.ours):
        if (
            not reused
            and last is not None
            and last.matched_at is not None
            and now - last.matched_at >= ARRIVED_HOLD_S
            and _moved_away(old, new, last.target, caps)
        ):
            # A person at a dimmer while our render runs: the lamp kept our on/off but
            # its brightness or colour moved away from our target. Verification would
            # re-send over them, up to six times. A flip stays noise (a bridge's
            # optimistic off corrected later); a step towards the target is a transition;
            # a report still carrying our context is the lamp answering our call (one
            # that clamps what it was sent) and is left to the verification.
            #
            # Only a lamp that has already ARRIVED at our target can be moved away from
            # it by a person: it has shown the target for ARRIVED_HOLD_S with no other
            # report since (``last.matched_at``, kept by the engine). Until then a move
            # away is the lamp still answering - an echo of a remembered or power-on
            # level before the fade. This is measured from the lamp's own reports, so it
            # holds however late the answer is, which a window after our command cannot:
            # a Matter-over-Thread globe answered 0.7-3 s after the command on 2026-09-16
            # and 5.2 s after it on 2026-09-19, past Home Assistant's 5 s context reuse,
            # and was taken back both times - it then sat at the nightlight level all
            # day. The price: a person who moves a dimmer before the lamp has arrived is
            # sent over once by the verification, and is recognised from their next move.
            return Verdict(EXTERNAL, source=SRC_DEVICE)
        return Verdict(NOISE)       # our render's verification judges it (and retries)
    if (
        not reused
        and _late(rec, old, new, caps, now)
        # Someone else's command is only ever repaired by a later layers.* call on
        # the lamp. Re-sending a reverted ON would light a lamp that may have been
        # switched off on purpose: that flip is judged as a change instead.
        and (last.ours or not raises_output(new, last.target))
    ):
        return Verdict(FAILED_DELIVERY, source=last.source, ours=last.ours, command=last.target)
    if rt.render_alive:
        return Verdict(NOISE)
    if _follow_up(rec, old, new, now):
        return Verdict(FOLLOW_UP, source=rec.last_external.source)
    return Verdict(DEBOUNCE)


# --------------------------------------------------------------------------- #
# 6.2 Debounce
# --------------------------------------------------------------------------- #


def _colour_changed(a: Observed, b: Observed) -> bool:
    """The two reports show a different colour (compared as ``close`` compares it)."""
    if a.color_mode != b.color_mode:
        return True
    if a.kelvin is not None and b.kelvin is not None:
        return abs(a.kelvin - b.kelvin) > TOL_KELVIN
    if a.xy is not None and b.xy is not None:
        return _xy_distance(a.xy, b.xy) > TOL_XY
    return (a.kelvin, a.xy, a.hs) != (b.kelvin, b.xy, b.hs)


def changed_groups(before: Observed | None, after: Observed) -> frozenset[str] | None:
    """The attribute groups a no-context change changed, for a take-back (SPEC 6.2).

    ``None`` (every group the lamp shows) when the report before is unknown or
    the lamp flipped on/off: then the device decided everything it shows.
    Otherwise ``state`` plus ``brightness`` and/or ``color``, whichever moved:
    a dimmer turning a lamp down does not make a signal colour it still shows
    the lamp's base.
    """
    if before is None or _on_off(before) is None or _on_off(after) is None:
        return None
    if before.state != after.state or after.state != ON:
        return None
    groups = {GROUP_STATE}
    if (before.brightness is None) != (after.brightness is None) or (
        before.brightness is not None
        and after.brightness is not None
        and abs(before.brightness - after.brightness) > TOL_BRIGHTNESS
    ):
        groups.add(GROUP_BRIGHTNESS)
    if _colour_changed(before, after):
        groups.add(GROUP_COLOR)
    return frozenset(groups) if len(groups) > 1 else None


def settle_debounce(rec: Record, current: Observed | None, caps: Caps,
                    now: float | None = None) -> Verdict:
    """Judge a no-context change that has held ``DEBOUNCE_S`` (SPEC 6.2).

    Close to ``rec.observed_prev`` (a blip that came back) or matching the
    projection of the effective command -> ``REREPORT``; otherwise ``EXTERNAL``
    from the device, taking the groups that changed from ``observed_prev``
    (``changed_groups``). The effective command is taken at ``now``, or at
    ``current.at`` when not given. A lamp that is away again -> ``IGNORE``
    (the transport path has it).
    """
    if current is None or not current.available:
        return Verdict(IGNORE)
    if close(current, rec.observed_prev):
        return Verdict(REREPORT)
    effective = resolve(rec, current.at if now is None else now).command
    if effective is not None and matches(current, project(effective, caps), caps) == MATCH_YES:
        return Verdict(REREPORT)
    return Verdict(EXTERNAL, source=SRC_DEVICE, groups=changed_groups(rec.observed_prev, current))


# --------------------------------------------------------------------------- #
# 6.3 Intent path
# --------------------------------------------------------------------------- #


def _intent(rec: Record, call: CallInfo, command: Command,
            groups: frozenset[str] | None) -> Verdict:
    return Verdict(EXTERNAL_INTENT, source=call.source, call=call, groups=groups,
                   replay=_replay(rec, call), command=command)


def _already_shows(shown: Observed, command: Command, groups: frozenset[str], caps: Caps) -> bool:
    """The lamp is on and shows the call's brightness/colour, as the lamp would take them.

    At least one of them must be something the lamp takes and ``matches``
    compares: a colour a CT-only lamp cannot show, hs/rgb (never compared), or
    a brightness to an on/off lamp would otherwise "match" vacuously.
    """
    wanted = Command(
        ON,
        command.brightness if GROUP_BRIGHTNESS in groups else None,
        command.color if GROUP_COLOR in groups else None,
    )
    call = project(wanted, caps)
    return bool(compared_groups(shown, call, caps)) and matches(shown, call, caps) == MATCH_YES


def classify_call(rec: Record, call: CallInfo, caps: Caps, now: float) -> Verdict | None:
    """The intent path (SPEC 6.3): a foreign call that no ``state_changed`` may reveal.

    - The lamp is unavailable and the intent is known -> ``EXTERNAL_INTENT``
      with the call's command (the engine records it and sets
      ``owed(missed=True)``). A toggle or an unknown intent has nothing to record.
    - ``turn_off`` (or an off intent) and the lamp shows off -> ``EXTERNAL_INTENT(off)``.
    - ``turn_on`` naming brightness and/or colour the lamp already shows (and
      that the lamp can take and be compared on) -> ``EXTERNAL_INTENT``.
    - Anything else, a toggle, an unknown intent, or a call that does not
      reach the lamp -> ``None``: the state path will see it.

    The verdict's groups are the call's, plus ``state``: a ``turn_on`` decides it.
    """
    if not _reaches(call, rec.entity_id):
        return None
    shown = rec.observed
    wants_off = call.service == SERVICE_TURN_OFF or (call.command is not None and call.command.is_off)
    off_groups = frozenset({GROUP_STATE})

    if not rec.available or (shown is not None and not shown.available):
        if wants_off:
            return _intent(rec, call, OFF_COMMAND, off_groups)
        if call.service != SERVICE_TURN_ON or call.command is None:
            return None
        return _intent(rec, call, call.command, _with_state(call))
    if shown is None:
        return None
    if wants_off:
        return _intent(rec, call, OFF_COMMAND, off_groups) if shown.state == OFF else None
    if call.service != SERVICE_TURN_ON or call.command is None:
        return None
    if not call.groups & {GROUP_BRIGHTNESS, GROUP_COLOR}:
        return None     # a bare turn_on names no attribute to recognise
    if _already_shows(shown, call.command, call.groups, caps):
        return _intent(rec, call, call.command, _with_state(call))
    return None


# --------------------------------------------------------------------------- #
# 6.4 Returns
# --------------------------------------------------------------------------- #


def decide_return(rec: Record, shown: Observed | None, caps: Caps, now: float) -> ReturnDecision:
    """What to do with a lamp that came back showing ``shown`` (SPEC 6.4, ``O`` there).

    ``E`` is the effective command at ``now``; ``P`` is ``rec.p_at_drop``, what
    the lamp showed when it went away (unknown when missing or unavailable).
    "Lights" means sending ``E`` would turn ``O`` on or brighten it
    (``raises_output``). First match wins:

    - ``O`` missing or unavailable -> ``WAIT`` (keep ``owed``);
    - ``E`` is ``None`` -> ``RECORD``;
    - ``O`` matches ``project(E)`` -> ``NOTHING``;
    - ``P`` known and not ``close(O, P)``: it came back different -> ``EXTERNAL``
      from the device.

    From here nobody touched it (``P`` close) or nobody can tell (``P`` unknown):

    - nothing owed and ``diverged`` is ``manual_keep``/``unsynced`` -> ``NOTHING``:
      only ``layers.sync`` pushes those;
    - a live ``set`` layer holds the lamp -> ``SEND``;
    - an ``adjust`` layer is active and ``O`` is on (sending only changes its
      attributes), with ``P`` known or not lighting -> ``SEND``;
    - ``owed``, and sending does not light, or ``P`` is known and the owed
      render is at most ``OWED_ON_MAX_AGE_S`` old -> ``SEND``;
    - otherwise -> ``RECORD``.

    So without evidence that the lamp was untouched, only a holding layer or a
    render that cannot light it is sent, and an owed off is never lost.
    """
    if shown is None or not shown.available:
        return ReturnDecision(WAIT)
    effective = resolve(rec, now).command
    if effective is None:
        return ReturnDecision(RECORD)
    if matches(shown, project(effective, caps), caps) == MATCH_YES:
        return ReturnDecision(NOTHING)
    before = rec.p_at_drop
    known = before is not None and before.available
    if known and not close(shown, before):
        return ReturnDecision(EXTERNAL, source=SRC_DEVICE)

    owed = rec.owed
    if owed is None and rec.diverged in (DIV_MANUAL_KEEP, DIV_UNSYNCED):
        return ReturnDecision(NOTHING)
    if state_holder(rec, now) is not None:
        return ReturnDecision(SEND)
    lights = raises_output(shown, effective)
    if active_layer(rec, now) is not None and shown.state == ON and (known or not lights):
        return ReturnDecision(SEND)
    if owed is not None and (not lights or (known and now - owed.since <= OWED_ON_MAX_AGE_S)):
        return ReturnDecision(SEND)
    return ReturnDecision(RECORD)
