"""The Layers engine: one record per lamp, the listeners that feed it, and the
rules for when a lamp may be sent a command.

The decisions themselves are pure functions in ``logic/`` (resolve, policy,
classify). This module turns Home Assistant events into their inputs, applies
their outcomes, keeps the timers, and starts renders. It never sends a command
itself; ``_render`` hands the lamp to the renderer, and only the paths listed in
docs/SPEC.md section 7.3 call it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
import logging
from typing import Any

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, EVENT_CALL_SERVICE, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import (
    CALLBACK_TYPE,
    CoreState,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BASE_KEEP,
    CONF_DEFAULT_POLICY,
    CONF_EDIT_ACTIVE,
    CONF_ENTITIES,
    CONF_REASSERT,
    DECISION_BUFFER,
    DOMAIN,
    EVENT_EXTERNAL,
    MANAGED_DOMAINS,
    RESULT_IN_SYNC,
    RESULT_PENDING,
    RESULT_QUEUED,
    RESULT_SHADOW,
    RESULT_SKIPPED_NOT_ENROLLED,
    RESULT_UNCHANGED,
    STALE_UNAVAILABLE_S,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_PENDING,
    STATUS_SHADOW,
)
from .logic import classify as cl
from .logic import policy as pol
from .logic.capability import (
    MATCH_YES,
    caps_from_attrs,
    close,
    matches,
    observed_from_state,
    observed_to_command,
    project,
)
from .logic.model import (
    CALL_MEMORY_S,
    Caps,
    DEBOUNCE_S,
    DIV_DELIVERY,
    DIV_MANUAL_KEEP,
    DIV_REPAIRED_BY_TARGETED_CALL,
    DIV_UNSYNCED,
    LATE_WINDOW_DEFAULT_S,
    LATE_WINDOW_S,
    LastCommand,
    OFF,
    ON,
    Observed,
    Owed,
    POLICY_BASE_KEEP_LAYERS,
    POLICY_EDIT_ACTIVE,
    POLICY_REASSERT,
    POLICY_TAKE_BACK,
    REPLAY_QUIET_S,
    RETURN_SETTLE_S,
    Record,
    SRC_AUTOMATION,
    SRC_DEVICE,
    SRC_USER,
    STARTUP_GRACE_S,
    SetRequest,
)
from .logic.resolve import resolve
from .render import Renderer, RenderJob
from .store import LayersStore
from .targets import normalise_call

_LOGGER = logging.getLogger(__name__)

SIGNAL_UPDATE = f"{DOMAIN}_update"
OURS_KEEP_S = 10.0          # Home Assistant reuses a command's context for 5 s of state writes
FIRST_SEEN_KEEP_S = 64.0    # a retry loop re-sends one press for up to about a minute
LIGHT_SERVICES = frozenset({"turn_on", "turn_off", "toggle"})
HANDOFF_OURS = "ours_handoff"   # hass.data[DOMAIN] key: our live contexts, across a reload
BASE_SRC_OBSERVED = "observed"  # base_source when a set on a lamp with no base learns what it shows
SAVE_DELAY_S = 2.0              # model changes
SAVE_LAZY_S = 60.0              # observed-only changes

# Divergences that only layers.sync repairs: no deferred render pushes them (SPEC 2).
DIV_SYNC_ONLY = frozenset({DIV_MANUAL_KEEP, DIV_UNSYNCED})


@callback
def _light_call_filter(event_data: dict[str, Any]) -> bool:
    return event_data.get("domain") in MANAGED_DOMAINS and event_data.get("service") in LIGHT_SERVICES


def _on_off(obs: Observed | None) -> str | None:
    return obs.state if obs is not None and obs.state in (ON, OFF) else None


def _keeps_state(command: LastCommand) -> bool:
    """A command known not to turn the lamp on or off (it cannot be reversed by a flip)."""
    target = command.target
    return (
        target is not None
        and target.state in (ON, OFF)
        and command.from_state == target.state
    )


class Engine:
    """Everything Layers knows and does for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.store = LayersStore(hass)
        opts = entry.options
        self.enrolled: set[str] = set(opts.get(CONF_ENTITIES, []))
        self.default_policy: str = opts.get(CONF_DEFAULT_POLICY, POLICY_TAKE_BACK)
        self.edit_active: set[str] = set(opts.get(CONF_EDIT_ACTIVE, []))
        self.base_keep: set[str] = set(opts.get(CONF_BASE_KEEP, []))
        self.reassert: set[str] = set(opts.get(CONF_REASSERT, []))
        self.records: dict[str, Record] = {}
        self.apply = False
        self.seq = 0
        self.renderer = Renderer(hass, self)
        self.decisions: deque[dict[str, Any]] = deque(maxlen=DECISION_BUFFER)
        self.in_grace = True
        self._platforms: dict[str, str] = {}
        self._ours: dict[str, dict[str, cl.OurCommand]] = {}
        self._calls: dict[str, cl.CallInfo] = {}        # context id -> latest call under it
        self._call_last: dict[str, float] = {}           # context id -> last sighting
        self._room_calls: dict[str, cl.CallInfo] = {}
        # (context id, service, intent, groups, lamps) -> (first, last) sighting. A retry
        # loop re-sends the same call; a script's next step under the same context does not.
        self._call_seen: dict[tuple[Any, ...], tuple[float, float]] = {}
        self._timers: dict[tuple[str, str], CALLBACK_TYPE] = {}
        self._ttl_unsub: CALLBACK_TYPE | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._failed: set[str] = set()
        self._late_retried: set[str] = set()
        self._releases: dict[str, CALLBACK_TYPE] = {}
        self._save_due: float | None = None
        self._setup_before_start = hass.state is not CoreState.running
        self._stopping = False

    # ================================================================ lifecycle

    async def async_start(self) -> None:
        data = await self.store.async_load() or {}
        self.seq = int(data.get("seq", 0))
        self.apply = bool(data.get("apply", False))
        stored: dict[str, Any] = data.get("entities", {})
        now = self.now()
        registry = er.async_get(self.hass)
        for eid in sorted(self.enrolled):
            rec = Record.from_json(eid, stored[eid]) if eid in stored else Record(eid)
            stored_obs = rec.observed
            state = self.hass.states.get(eid)
            current = self._obs(state, now) if state is not None else None
            rec.observed = current
            rec.available = bool(current and current.available)
            if rec.p_at_drop is None and stored_obs is not None and stored_obs.available:
                # What the lamp showed when Layers stopped: the downtime counts as time
                # away, so an owed render is only honoured, and a layered lamp is only
                # left unsynced, if nobody touched the lamp in between (SPEC 2, 7.4).
                rec.p_at_drop = stored_obs
            self.records[eid] = rec
            reg_entry = registry.async_get(eid)
            self._platforms[eid] = reg_entry.platform if reg_entry else ""
        self._take_over_ours()
        if self.enrolled:
            self._unsubs.append(
                async_track_state_change_event(self.hass, sorted(self.enrolled), self._on_state)
            )
        self._unsubs.append(
            self.hass.bus.async_listen(EVENT_CALL_SERVICE, self._on_call, event_filter=_light_call_filter)
        )
        self._unsubs.append(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_registry_update,
                event_filter=self._renamed_lamp,
            )
        )
        self._unsubs.append(async_at_started(self.hass, self._on_started))
        self._unsubs.append(self.hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, self._on_hass_stop))

    def _cancel_all_timers(self) -> None:
        for unsub in [*self._timers.values(), *self._releases.values()]:
            unsub()
        self._timers.clear()
        self._releases.clear()
        if self._ttl_unsub:
            self._ttl_unsub()
            self._ttl_unsub = None
        self.renderer.cancel_all()

    @callback
    def _on_hass_stop(self, _event: Event) -> None:
        """Home Assistant is stopping: drop timers and renders; the store flushes itself."""
        self._stopping = True
        self._cancel_all_timers()

    async def async_stop(self) -> None:
        self._stopping = True
        self._hand_over_ours()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._cancel_all_timers()
        self._save_due = None
        await self.store.async_save(self._data_to_save())

    def _hand_over_ours(self) -> None:
        """Leave our live contexts to the next engine (a reload): Home Assistant keeps
        stamping the lamps' writes with them for 5 s, and one the next engine does not
        know would read as an automation's change and take back our own layer."""
        now = self.now()
        live = {
            eid: {cid: oc for cid, oc in ours.items() if now - oc.at <= OURS_KEEP_S}
            for eid, ours in self._ours.items()
        }
        self.hass.data.setdefault(DOMAIN, {})[HANDOFF_OURS] = {e: o for e, o in live.items() if o}

    def _take_over_ours(self) -> None:
        handed: dict[str, dict[str, cl.OurCommand]] = (
            self.hass.data.get(DOMAIN, {}).pop(HANDOFF_OURS, None) or {}
        )
        now = self.now()
        for eid, ours in handed.items():
            if eid not in self.records:
                continue
            for context_id, ours_cmd in ours.items():
                if now - ours_cmd.at <= OURS_KEEP_S:
                    self.add_ours(eid, context_id, ours_cmd.target, ours_cmd.at)
                    self.release_ours_later(eid, context_id)

    @callback
    def _on_started(self, _hass: HomeAssistant) -> None:
        grace = STARTUP_GRACE_S if self._setup_before_start else RETURN_SETTLE_S
        self._later("grace", "", grace, self._end_grace)

    @callback
    def _end_grace(self, _eid: str = "") -> None:
        """Startup is over: reconcile the model with what the lamps show. Records only,
        except for layers that expired while Home Assistant was down and owed renders."""
        self.in_grace = False
        now = self.now()
        try:
            for eid, rec in list(self.records.items()):
                try:
                    self._end_grace_lamp(eid, rec, now)
                except Exception:  # noqa: BLE001 — one lamp must not stop the others
                    _LOGGER.exception("layers: could not reconcile %s at the end of the grace", eid)
                    self._mark_untrusted(eid)
        finally:
            self._schedule_ttl()
            self.save()
            self.notify()

    def _end_grace_lamp(self, eid: str, rec: Record, now: float) -> None:
        caps = self.caps(eid)
        shown = rec.observed if rec.available else rec.p_at_drop
        # 1. Layers that expired while Home Assistant was down. An allowed render is
        #    Layers' own decision (SPEC 7.3 c), neither an old debt for decide_return
        #    nor "base := what it shows" (it still shows the layer, e.g. a signal colour).
        if self._has_expired(rec, now):
            res = pol.expire(rec, now, shown, caps)
            self._decision(eid, "expiry", expired=list(res.expired), render=res.render,
                           at_startup=True)
            if res.expired:
                if res.render:
                    self._render(eid, reason="expiry", parent_id=None, deferred=True)
                else:
                    self._drop_stale_render(eid)
                if rec.available:
                    rec.p_at_drop = None
                return
        if rec.available:
            if self.renderer.alive(eid):
                # 2. A layers.* call during the grace started a render: Layers' own
                #    decision, which its verification judges. Its owed is no old debt.
                rec.p_at_drop = None
                return
            if rec.owed is not None and rec.observed is not None:
                # 3. An old debt, judged like a return.
                self._return_decision(rec, rec.observed, caps, now, reason="startup")
            elif not rec.layers:
                # 4. Nothing layered: what it shows is its base (so it shows it).
                if rec.observed is not None:
                    pol.record_observed_as_base(rec, rec.observed, caps, now, "startup")
                    rec.diverged = None
            else:
                # 5. Layered and not showing its command: changed while Layers was down
                #    (a change like any other), or unsynced (only layers.sync pushes it).
                self._startup_mismatch(rec, caps, now)
            rec.p_at_drop = None
        # A lamp that is away is judged by decide_return when it comes back.

    def _startup_mismatch(self, rec: Record, caps: Caps, now: float) -> None:
        call = project(resolve(rec, now).command, caps)
        shown = rec.observed
        if call is None or shown is None or matches(shown, call, caps) == MATCH_YES:
            return
        before = rec.p_at_drop
        if before is not None and before.available and not close(shown, before):
            self._decision(rec.entity_id, "startup:external")
            self._external(rec, cl.Verdict(cl.EXTERNAL, source=SRC_DEVICE), shown, caps, now)
            return
        rec.diverged = DIV_UNSYNCED

    # ================================================================ helpers

    @staticmethod
    def now() -> float:
        return dt_util.utcnow().timestamp()

    @staticmethod
    def _obs(state: State | None, now: float | None = None) -> Observed | None:
        if state is None:
            return None
        return observed_from_state(state.state, state.attributes, now or state.last_updated_timestamp)

    def platform(self, eid: str) -> str:
        return self._platforms.get(eid, "")

    def caps(self, eid: str) -> Caps:
        state = self.hass.states.get(eid)
        if state is None:
            return Caps(platform=self.platform(eid))
        return caps_from_attrs(state.attributes, self.platform(eid))

    def policy_for(self, eid: str) -> str:
        if eid in self.edit_active:
            return POLICY_EDIT_ACTIVE
        if eid in self.base_keep:
            return POLICY_BASE_KEEP_LAYERS
        if eid in self.reassert:
            return POLICY_REASSERT
        return self.default_policy

    def holds_layers(self, eid: str) -> bool:
        rec = self.records.get(eid)
        return bool(rec and rec.layers)

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def _later(self, kind: str, eid: str, delay: float, action: Callable[[str], None]) -> None:
        key = (kind, eid)
        self._cancel(kind, eid)

        @callback
        def _fire(_now: Any) -> None:
            self._timers.pop(key, None)
            try:
                action(eid)
            except Exception:  # noqa: BLE001 — a timer must never take the engine down
                _LOGGER.exception("layers: %s timer for %s failed", kind, eid or "all")
                if eid in self.records:
                    self._mark_untrusted(eid)

        self._timers[key] = async_call_later(self.hass, delay, _fire)

    def _cancel(self, kind: str, eid: str) -> None:
        unsub = self._timers.pop((kind, eid), None)
        if unsub:
            unsub()

    def _pending(self, kind: str, eid: str) -> bool:
        return (kind, eid) in self._timers

    def _mark_untrusted(self, eid: str) -> None:
        """Something failed on this lamp: deferred renders skip it until layers.sync."""
        rec = self.records.get(eid)
        if rec is None:
            return
        rec.untrusted = True
        self.renderer.cancel(eid)

    def _runtime(self, eid: str) -> cl.Runtime:
        self._prune_calls()
        return cl.Runtime(
            ours=dict(self._ours.get(eid, {})),
            render_alive=self.renderer.alive(eid),
            calls=dict(self._calls),
            room_call=self._room_calls.get(eid),
            returning=self._pending("return", eid),
        )

    def _prune_calls(self) -> None:
        """Calls attribute state changes for CALL_MEMORY_S after they were last seen;
        their first sighting is remembered longer, for the replay rule."""
        now = self.now()
        cutoff = now - CALL_MEMORY_S
        for key in [k for k, last in self._call_last.items() if last < cutoff]:
            del self._call_last[key]
            self._calls.pop(key, None)
        for lamp in [l for l, c in self._room_calls.items() if c.context_id not in self._call_last]:
            del self._room_calls[lamp]
        for key in [k for k, (_f, last) in self._call_seen.items() if last < now - FIRST_SEEN_KEEP_S]:
            del self._call_seen[key]

    def _decision(self, eid: str, kind: str, **extra: Any) -> None:
        self.decisions.append(
            {"at": dt_util.utcnow().isoformat(), "entity_id": eid, "kind": kind, **extra}
        )

    def notify(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    # ================================================================ ours

    def add_ours(self, eid: str, context_id: str, target: Any, now: float) -> None:
        self._ours.setdefault(eid, {})[context_id] = cl.OurCommand(context_id, target, now)

    def release_ours_later(self, eid: str, context_id: str) -> None:
        @callback
        def _release(_now: Any) -> None:
            self._releases.pop(context_id, None)
            ours = self._ours.get(eid)
            if ours is not None:
                ours.pop(context_id, None)

        old = self._releases.pop(context_id, None)
        if old:
            old()
        if self._stopping:  # a render cancelled by shutdown: nothing left to recognise
            _release(None)
            return
        self._releases[context_id] = async_call_later(self.hass, OURS_KEEP_S, _release)

    def _is_ours(self, context_id: str | None) -> bool:
        return bool(context_id) and any(context_id in d for d in self._ours.values())

    # ================================================================ persistence

    def _data_to_save(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "apply": self.apply,
            "entities": {
                eid: rec.to_json() for eid, rec in self.records.items() if rec.worth_persisting()
            },
        }

    def _data_for_store(self) -> dict[str, Any]:
        self._save_due = None     # called when the Store actually writes
        return self._data_to_save()

    def save(self, *, immediate: bool = False, lazy: bool = False) -> None:
        """Schedule a write: now, in 2 s (model changes) or in 60 s (observations).

        Home Assistant's Store keeps one timer, and a later call with a longer delay
        pushes an earlier write out to it. So a longer delay is never asked for while
        a sooner write is pending: that write takes the latest data anyway.
        """
        delay = 0.0 if immediate else (SAVE_LAZY_S if lazy else SAVE_DELAY_S)
        due = self.hass.loop.time() + delay
        if self._save_due is not None and self._save_due <= due:
            return
        self._save_due = due
        self.store.async_delay_save(self._data_for_store, delay)

    async def _save_now(self) -> None:
        self._save_due = None     # this write replaces any pending one
        await self.store.async_save(self._data_to_save())

    # ================================================================ entity renames

    @callback
    def _renamed_lamp(self, event_data: dict[str, Any]) -> bool:
        return event_data.get("action") == "update" and event_data.get("old_entity_id") in self.enrolled

    @callback
    def _on_registry_update(self, event: Event) -> None:
        """An enrolled lamp was renamed: its record and options follow it, then reload."""
        old, new = event.data["old_entity_id"], event.data["entity_id"]
        _LOGGER.info("layers: %s was renamed to %s; following it", old, new)
        rec = self.records.pop(old, None)
        if rec is not None:
            rec.entity_id = new
            self.records[new] = rec
        self.renderer.cancel(old)
        self.enrolled.discard(old)
        self.enrolled.add(new)
        self._platforms[new] = self._platforms.pop(old, "")
        options = dict(self.entry.options)
        for key in (CONF_ENTITIES, CONF_EDIT_ACTIVE, CONF_BASE_KEEP, CONF_REASSERT):
            options[key] = [new if e == old else e for e in options.get(key, [])]
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

    # ================================================================ state events

    @callback
    def _on_state(self, event: Event[EventStateChangedData]) -> None:
        eid = event.data["entity_id"]
        rec = self.records.get(eid)
        if rec is None:
            return
        try:
            self._handle_state(rec, event)
        except Exception:  # noqa: BLE001 — one lamp's bad event must not stop the others
            _LOGGER.exception("layers: could not classify a change on %s", eid)
            self._mark_untrusted(eid)

    def _handle_state(self, rec: Record, event: Event[EventStateChangedData]) -> None:
        eid = rec.entity_id
        now = self.now()
        old_state, new_state = event.data["old_state"], event.data["new_state"]
        old, new = self._obs(old_state, now), self._obs(new_state, now)
        ctx = new_state.context if new_state is not None else event.context
        ev = cl.StateEvent(eid, old, new, ctx.id, ctx.parent_id, ctx.user_id, now)
        caps = self.caps(eid)
        verdict = cl.classify_state(rec, ev, self._runtime(eid), caps, now)
        kind = verdict.kind
        self._decision(eid, kind, source=verdict.source, replay=verdict.replay)

        if kind == cl.IGNORE:
            return
        if kind in (cl.TRANSPORT_DOWN, cl.GONE):
            # GONE: the entity was removed (its integration reloading, a rename, a
            # deletion). Either way the lamp is away until it reports again.
            if rec.observed is not None and rec.observed.available and rec.p_at_drop is None:
                rec.p_at_drop = rec.observed
            rec.available = False
            rec.observed = new
            self._cancel("debounce", eid)
            rec.observed_prev = None
            self._cancel("return", eid)
            self.save()
            self.notify()
            return
        if kind == cl.FIRST and (self.in_grace or rec.available):
            rec.observed = new
            rec.available = bool(new and new.available)
            return
        if kind in (cl.TRANSPORT_UP, cl.FIRST):
            # FIRST here: the entity is back after it was removed, or appeared only
            # after the grace. Either way it is a return, judged once it settles.
            rec.available = True
            rec.observed = new
            if not self.in_grace:
                self._later("return", eid, RETURN_SETTLE_S, self._judge_return)
            self.notify()
            return

        # From here the lamp is available and its state changed.
        rec.observed = new
        last = rec.last_command
        if last is not None and last.ours:
            # Since when has the lamp shown our command's target with nothing else in
            # between? Any other report restarts it (classify_state: ARRIVED_HOLD_S).
            if not cl.shows_target(new, last.target, caps):
                last.matched_at = None
            elif last.matched_at is None:
                last.matched_at = now
        if kind == cl.OURS:
            if new is not None and new.state == OFF:
                pol.lift_on_off(rec)
            self.save(lazy=True)
            return
        if kind == cl.EXTERNAL:
            self._external(rec, verdict, new, caps, now, user_id=ctx.user_id)
            return
        if self._pending("return", eid):
            # The lamp just came back and is still settling: its reports are what the
            # return decision will look at, not changes to judge one by one.
            self.save(lazy=True)
            return
        if self.in_grace:
            # Integrations are still connecting and re-reporting: no-context
            # changes during startup are observations, never manual changes.
            self.save(lazy=True)
            return
        if kind == cl.FAILED_DELIVERY:
            self._failed_delivery(rec, verdict, old)
            return
        if kind == cl.NOISE:
            self.save(lazy=True)
            return
        if kind == cl.FOLLOW_UP:
            last = rec.last_external
            follow = cl.Verdict(cl.EXTERNAL, source=last.source, groups=last.groups,
                                replay=last.policy == pol.POLICY_REPLAY)
            self._external(rec, follow, new, caps, now, user_id=last.user_id, policy=last.policy,
                           follow_up=True)
            return
        if kind == cl.DEBOUNCE:
            self._start_debounce(rec, old)
            return

    def _start_debounce(self, rec: Record, old: Observed | None) -> None:
        eid = rec.entity_id
        if not self._pending("debounce", eid):
            rec.observed_prev = old
        self._later("debounce", eid, DEBOUNCE_S, self._settle_debounce)

    def _failed_delivery(self, rec: Record, verdict: cl.Verdict, old: Observed | None) -> None:
        eid = rec.entity_id
        # A no-context change was already pending: judge it first, it may be a person's.
        self._flush_debounce(eid)
        if verdict.ours:
            if eid in self._late_retried:
                # One late retry per command: a second reversal is not a bridge's.
                self._decision(eid, "late_retry_spent")
                self._start_debounce(rec, old)
                return
            self._late_retried.add(eid)
            self._render(eid, reason="retry", parent_id=None, deferred=True)
            self.save()
            return
        rec.diverged = DIV_DELIVERY
        self.save()
        self.notify()

    def _supersede(self, rec: Record) -> None:
        """Someone acted on the lamp: drop what Layers had pending on it (a debounce, a
        return decision, a replay re-render, our render and its late re-check)."""
        eid = rec.entity_id
        self._cancel("debounce", eid)
        rec.observed_prev = None
        self._cancel("return", eid)
        if rec.available:
            rec.p_at_drop = None    # it describes a lamp from before this change
        self._cancel("replay", eid)
        self.renderer.cancel(eid)

    def _external(self, rec: Record, verdict: cl.Verdict, new: Observed | None, caps: Caps,
                  now: float, user_id: str | None = None, *, policy: str | None = None,
                  follow_up: bool = False) -> None:
        eid = rec.entity_id
        shown = observed_to_command(new, caps) if new is not None else None
        call = verdict.call
        who = user_id if user_id else (call.user_id if call else None)
        if verdict.replay:
            # A retry loop's old press: base only, and the layers go back once it is quiet.
            self._cancel("debounce", eid)
            rec.observed_prev = None
            self._cancel("return", eid)
            rec.p_at_drop = None
            if shown is not None:
                self._replay(rec, shown, verdict.groups, verdict.source or SRC_AUTOMATION, now, who)
            return
        # Someone acted on the lamp: that supersedes a pending return decision, which
        # would otherwise re-send an owed command over what they just did.
        self._supersede(rec)
        if shown is None:
            return
        policy = policy or self.policy_for(eid)
        result = pol.apply_external(rec, shown, verdict.groups, verdict.source or SRC_DEVICE,
                                    policy, now, who, caps=caps)
        if new is not None and new.state == OFF:
            pol.lift_on_off(rec)
        self._failed.discard(eid)
        if result.dropped or (result.edited and not follow_up):
            self.hass.bus.async_fire(
                EVENT_EXTERNAL,
                {ATTR_ENTITY_ID: eid, "source": verdict.source, "policy": policy,
                 "dropped": list(result.dropped), "edited": result.edited},
            )
        if result.reassert:
            # The one policy that answers an external change with a command (7.3 g):
            # the device changed itself, the effective command goes back on it.
            self._decision(eid, "reassert", source=verdict.source)
            self._render(eid, reason="reassert", parent_id=None, deferred=True)
        self._schedule_ttl()
        self.save()
        self.notify()

    @callback
    def _settle_debounce(self, eid: str) -> None:
        rec = self.records[eid]
        current = rec.observed
        if current is None or not current.available:
            rec.observed_prev = None
            return
        if self.in_grace:
            rec.observed_prev = None
            return
        verdict = cl.settle_debounce(rec, current, self.caps(eid), self.now())
        self._decision(eid, verdict.kind, source=verdict.source, settled=True)
        if verdict.kind == cl.EXTERNAL:
            self._external(rec, verdict, current, self.caps(eid), self.now())
        rec.observed_prev = None

    def _flush_debounce(self, eid: str) -> None:
        """Judge a pending no-context change before Layers acts on the lamp."""
        if self._pending("debounce", eid):
            self._cancel("debounce", eid)
            self._settle_debounce(eid)

    @callback
    def _judge_return(self, eid: str) -> None:
        rec = self.records[eid]
        if rec.observed is None or not rec.available:
            return
        self._return_decision(rec, rec.observed, self.caps(eid), self.now(), reason="return")
        rec.p_at_drop = None
        self.save()
        self.notify()

    def _return_decision(self, rec: Record, current: Observed, caps: Caps, now: float,
                         reason: str) -> None:
        eid = rec.entity_id
        decision = cl.decide_return(rec, current, caps, now)
        kind = getattr(decision, "kind", decision)
        self._decision(eid, f"return:{kind}", reason=reason)
        if kind == cl.SEND:
            self._render(eid, reason=reason, parent_id=None, deferred=True)
        elif kind == cl.RECORD:
            pol.record_observed_as_base(rec, current, caps, now, reason)
            rec.owed = None
            self._drop_stale_render(eid)
        elif kind == cl.NOTHING:
            rec.owed = None
            self._drop_stale_render(eid)
        elif kind == cl.WAIT:
            return  # it went away again: still owed, the next return decides
        elif kind == cl.EXTERNAL:
            rec.owed = None
            self._external(rec, cl.Verdict(cl.EXTERNAL, source=SRC_DEVICE), current, caps, now)
        if current.state == OFF:
            pol.lift_on_off(rec)

    def _replay(self, rec: Record, shown: Any, groups: Any, source: str, now: float,
                user_id: str | None) -> None:
        """A retry loop re-applying a press made before this lamp's layers last changed:
        the base learns it, the layers stay, and once the loop goes quiet the layers
        are put back (SPEC 5.3 apply_replay, 7.3 f)."""
        eid = rec.entity_id
        pol.apply_replay(rec, shown, groups, source, now, user_id)
        self._decision(eid, "replay", source=source)
        self._drop_stale_render(eid)
        self._later("replay", eid, REPLAY_QUIET_S, self._after_replay)
        self.save()

    @callback
    def _after_replay(self, eid: str) -> None:
        self._flush_debounce(eid)
        self._render(eid, reason="replay", parent_id=None, deferred=True)
        self.save()

    # ================================================================ other integrations' calls

    @callback
    def _on_call(self, event: Event) -> None:
        ctx = event.context
        if self._is_ours(ctx.id):
            return
        if ctx.user_id:
            # A person's call is only acted on once Home Assistant would let them make
            # it: the call event fires before the service checks their permissions.
            self.entry.async_create_task(self.hass, self._on_user_call(event),
                                         f"{DOMAIN} user call", eager_start=True)
            return
        self._handle_call_safely(event, None)

    async def _on_user_call(self, event: Event) -> None:
        user = await self.hass.auth.async_get_user(event.context.user_id)
        if user is None:
            return  # Home Assistant refuses calls from an unknown user
        self._handle_call_safely(event, None if user.is_admin else user.permissions.check_entity)

    def _handle_call_safely(self, event: Event, may_control: Callable[[str, str], bool] | None) -> None:
        try:
            self._handle_call(event, may_control)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("layers: could not read a light call")

    def _handle_call(self, event: Event, may_control: Callable[[str, str], bool] | None) -> None:
        ctx = event.context
        service = event.data.get("service")
        domain = event.data.get("domain")
        # A light call cannot reach a switch and vice versa: Home Assistant refuses
        # the mismatch, so only lamps of the call's own domain are considered.
        lamps, via, intent, groups = normalise_call(
            self.hass, event.data.get("service_data") or {}, service,
            {lamp for lamp in self.enrolled if lamp.split(".", 1)[0] == domain},
        )
        if may_control is not None:
            # A lamp the caller may not control is left out: Home Assistant refuses it.
            lamps = frozenset(lamp for lamp in lamps if may_control(lamp, POLICY_CONTROL))
            via = via & lamps
        if not lamps:
            return
        now = self.now()
        source = SRC_USER if ctx.user_id else SRC_AUTOMATION
        key = (ctx.id, service, intent, groups, lamps)
        first, _last = self._call_seen.get(key, (now, now))
        self._call_seen[key] = (first, now)
        self._call_last[ctx.id] = now
        call = cl.CallInfo(ctx.id, source, ctx.user_id, service, intent, groups, lamps, via, first)
        self._calls[ctx.id] = call
        for lamp in via:
            self._room_calls[lamp] = call
        changed = False
        try:
            for lamp in sorted(lamps):
                try:
                    changed |= self._call_on_lamp(self.records[lamp], call, now)
                except Exception:  # noqa: BLE001 — one lamp must not stop the others
                    _LOGGER.exception("layers: could not apply a light call to %s", lamp)
                    self._mark_untrusted(lamp)
        finally:
            if changed:
                self._schedule_ttl()
                self.save()
                self.notify()

    def _call_on_lamp(self, rec: Record, call: cl.CallInfo, now: float) -> bool:
        lamp = rec.entity_id
        caps = self.caps(lamp)
        self._note_command(rec, LastCommand(now, ours=False, source=call.source, target=call.command,
                                            context_id=call.context_id,
                                            from_state=_on_off(rec.observed)), caps)
        verdict = cl.classify_call(rec, call, caps, now)
        if verdict is None:
            return False
        self._decision(lamp, verdict.kind, source=call.source, service=call.service)
        shown = verdict.command or call.command
        if shown is None:
            return False
        if verdict.replay:
            self._replay(rec, shown, verdict.groups, call.source, now, call.user_id)
            return True
        self._supersede(rec)
        policy = self.policy_for(lamp)
        result = pol.apply_external(rec, shown, verdict.groups, call.source, policy, now,
                                    call.user_id, caps=caps)
        if not rec.available:
            effective = resolve(rec, now).command
            rec.owed = (
                Owed(now, effective, turns_on=effective.is_on, missed=True)
                if effective is not None else None
            )
        elif rec.observed is not None and rec.observed.state == OFF:
            pol.lift_on_off(rec)
        if result.dropped or result.edited:
            self.hass.bus.async_fire(
                EVENT_EXTERNAL,
                {ATTR_ENTITY_ID: lamp, "source": call.source, "policy": policy,
                 "dropped": list(result.dropped), "edited": result.edited},
            )
        return True

    def _note_command(self, rec: Record, command: LastCommand, caps: Caps) -> None:
        """Record someone else's command as the lamp's last, for the late window.

        One known not to turn the lamp on or off cannot be reversed by a flip, so it
        does not hide a recent command that did: a bridge reverting our restore after
        an automation re-sent the same brightness is still our reverted restore.
        """
        prev = rec.last_command
        window = LATE_WINDOW_S.get(caps.platform, LATE_WINDOW_DEFAULT_S)
        if (
            prev is not None
            and prev.flips()
            and _keeps_state(command)
            and command.at - prev.at <= window
        ):
            return
        rec.last_command = command

    # ================================================================ rendering

    def _render(self, eid: str, *, reason: str, parent_id: str | None,
                transition: float | None = None, deferred: bool = False) -> str:
        """Hand the lamp its effective command. The ONLY place a command starts."""
        rec = self.records[eid]
        if deferred and rec.untrusted:
            self.renderer.cancel(eid)
            rec.owed = None             # only layers.sync pushes it: nothing is owed
            return RESULT_UNCHANGED
        if deferred and rec.diverged in DIV_SYNC_ONLY:
            rec.owed = None             # only layers.sync pushes these (SPEC 2)
            return RESULT_UNCHANGED
        now = self.now()
        res = resolve(rec, now)
        effective = res.command
        caps = self.caps(eid)
        call = project(effective, caps)
        if effective is None or call is None:
            # Nothing to send, and nothing may still be on its way (SPEC 7.3).
            self.renderer.cancel(eid)
            rec.owed = None
            self.notify()
            return RESULT_UNCHANGED
        job = self.renderer.job(eid)
        if (
            rec.observed is not None
            and rec.available
            and matches(rec.observed, call, caps) == MATCH_YES
            # A render of a different command may already have gone out, with the
            # lamp's report of it still to come: only a render of this command, or
            # none at all, lets the report stand for the lamp.
            and (job is None or job.call == call)
        ):
            self.renderer.cancel(eid)
            rec.owed = None
            rec.diverged = None     # it shows its effective command: nothing diverges
            self.notify()
            return RESULT_IN_SYNC
        if not self.apply:
            self.renderer.cancel(eid)
            rec.diverged = DIV_UNSYNCED
            rec.owed = None             # SPEC 7.5: with Apply off a lamp owes nothing
            self.notify()
            return RESULT_SHADOW
        if not deferred and rec.diverged in DIV_SYNC_ONLY:
            rec.diverged = None     # the owner's new command is what the lamp will show
        if not rec.available:
            self.renderer.cancel(eid)
            rec.owed = Owed(now, effective, turns_on=effective.is_on)
            self.notify()
            return RESULT_PENDING
        layer = res.active
        owner = rec.layers[layer].owner if layer in rec.layers else None
        if reason not in ("late_recheck", "retry"):
            self._late_retried.discard(eid)  # a new command gets its own late re-check
        # Our newer command supersedes a room call: its members' later reports are ours.
        self._room_calls.pop(eid, None)
        self.renderer.start(RenderJob(eid, effective, call, transition, parent_id, layer, owner, reason))
        return RESULT_QUEUED

    def render_wanted(self, job: RenderJob) -> bool:
        """Is ``job`` still what the lamp should be sent? (A render re-checks before retrying.)"""
        rec = self.records.get(job.entity_id)
        if rec is None or not self.apply:
            return False
        return project(resolve(rec, self.now()).command, self.caps(job.entity_id)) == job.call

    def _drop_stale_render(self, eid: str) -> None:
        """The record changed without a render: stop one that sends something else now."""
        job = self.renderer.job(eid)
        if job is None or self.render_wanted(job):
            return
        self.renderer.cancel(eid)
        rec = self.records[eid]
        if rec.owed is not None and not rec.owed.missed:
            rec.owed = None

    def render_succeeded(self, eid: str) -> None:
        self._failed.discard(eid)
        self.notify()

    def render_failed(self, eid: str) -> None:
        self._failed.add(eid)
        self.notify()

    def late_recheck(self, eid: str, call: Any) -> None:
        """Re-send once if a vendor bridge reverted a verified command later on."""
        rec = self.records.get(eid)
        if rec is None or not rec.available or rec.observed is None or eid in self._late_retried:
            return
        if self._pending("return", eid):
            return
        self._flush_debounce(eid)       # a person's pending change comes first
        caps = self.caps(eid)
        effective = resolve(rec, self.now()).command
        if project(effective, caps) != call:
            return  # the lamp's command changed since; nothing to re-check
        if matches(rec.observed, call, caps) != MATCH_YES:
            self._late_retried.add(eid)
            self._decision(eid, "late_recheck_failed")
            self._render(eid, reason="late_recheck", parent_id=None, deferred=True)
            self.save()

    # ================================================================ TTL

    @staticmethod
    def _has_expired(rec: Record, now: float) -> bool:
        return any(not layer.live(now) for layer in rec.layers.values()) or any(
            not t.live(now) for t in rec.tombstones.values()
        )

    def _schedule_ttl(self) -> None:
        if self._ttl_unsub:
            self._ttl_unsub()
            self._ttl_unsub = None
        if self.in_grace or self._stopping:
            return  # _end_grace reschedules
        times = [
            t for rec in self.records.values() if not rec.untrusted
            for t in [*(l.expires_at for l in rec.layers.values()),
                      *(s.expires_at for s in rec.tombstones.values())]
            if t is not None
        ]
        if not times:
            return
        when = dt_util.utc_from_timestamp(max(min(times), self.now() + 0.05))
        self._ttl_unsub = async_track_point_in_utc_time(self.hass, self._on_ttl, when)

    @callback
    def _on_ttl(self, _now: Any) -> None:
        self._ttl_unsub = None
        now = self.now()
        try:
            for eid, rec in list(self.records.items()):
                if rec.untrusted or not self._has_expired(rec, now):
                    continue
                try:
                    self._expire_lamp(eid, rec, now)
                except Exception:  # noqa: BLE001 — one lamp must not stop the others
                    _LOGGER.exception("layers: could not expire layers on %s", eid)
                    self._mark_untrusted(eid)
        finally:
            self._schedule_ttl()
            self.save()
            self.notify()

    def _expire_lamp(self, eid: str, rec: Record, now: float) -> None:
        self._flush_debounce(eid)       # a person's pending change is judged first
        caps = self.caps(eid)
        shown = rec.observed if rec.available else rec.p_at_drop
        result = pol.expire(rec, now, shown, caps)
        self._decision(eid, "expiry", expired=list(result.expired), render=result.render)
        if result.render:
            self._render(eid, reason="expiry", parent_id=None, deferred=True)
        else:
            self._drop_stale_render(eid)

    # ================================================================ services

    def _repair_needed(self, rec: Record) -> bool:
        return rec.diverged in DIV_REPAIRED_BY_TARGETED_CALL

    def validate_set(self, lamps: list[str], req: SetRequest) -> None:
        """Raise PolicyError before touching any lamp if one of them would refuse."""
        now = self.now()
        for eid in lamps:
            rec = self.records.get(eid)
            if rec is None:
                continue
            probe = Record.from_json(eid, rec.to_json())
            pol.apply_set(probe, req, now, lambda: 0)

    def _learn_base(self, rec: Record, now: float) -> None:
        """A lamp with no layer and no known base (the startup or reload grace, or a
        return still settling) shows its base: learn it before layering it, so that
        releasing the layer restores it instead of leaving the layer's output."""
        if rec.base is not None or rec.layers or not rec.available:
            return
        if rec.observed is not None and rec.observed.available:
            pol.record_observed_as_base(rec, rec.observed, self.caps(rec.entity_id), now,
                                        BASE_SRC_OBSERVED)

    def set_layer(self, lamps: list[str], req: SetRequest, *, transition: float | None,
                  parent_id: str | None) -> dict[str, str]:
        now = self.now()
        results: dict[str, str] = {}
        for eid in lamps:
            rec = self.records.get(eid)
            if rec is None:
                results[eid] = RESULT_SKIPPED_NOT_ENROLLED
                continue
            self._flush_debounce(eid)
            self._learn_base(rec, now)
            before = resolve(rec, now).command
            outcome = pol.apply_set(rec, req, now, self.next_seq)
            if outcome.result.startswith("skipped"):
                results[eid] = outcome.result
                continue
            after = resolve(rec, now).command
            if after != before or self._repair_needed(rec):
                results[eid] = self._render(eid, reason="set", parent_id=parent_id,
                                            transition=transition)
            else:
                results[eid] = RESULT_UNCHANGED
        self._schedule_ttl()
        self.save()
        self.notify()
        return results

    def clear_layer(self, lamps: list[str] | None, layer: str, *, transition: float | None,
                    parent_id: str | None) -> dict[str, str]:
        now = self.now()
        if lamps is None:
            lamps = [e for e, r in self.records.items() if layer in r.layers or layer in r.tombstones]
        results: dict[str, str] = {}
        for eid in lamps:
            rec = self.records.get(eid)
            if rec is None:
                results[eid] = RESULT_SKIPPED_NOT_ENROLLED
                continue
            self._flush_debounce(eid)
            before = resolve(rec, now).command
            pol.apply_clear(rec, layer, now)
            after = resolve(rec, now).command
            if after != before or self._repair_needed(rec):
                results[eid] = self._render(eid, reason="clear", parent_id=parent_id,
                                            transition=transition)
            else:
                results[eid] = RESULT_UNCHANGED
        self._schedule_ttl()
        self.save()
        self.notify()
        return results

    def sync(self, lamps: list[str] | None, *, parent_id: str | None) -> dict[str, str]:
        results: dict[str, str] = {}
        for eid in lamps if lamps is not None else sorted(self.records):
            rec = self.records.get(eid)
            if rec is None:
                results[eid] = RESULT_SKIPPED_NOT_ENROLLED
                continue
            self._flush_debounce(eid)
            rec.untrusted = False       # an owner's sync is the way back
            if rec.diverged in DIV_SYNC_ONLY:
                rec.diverged = None
            results[eid] = self._render(eid, reason="sync", parent_id=parent_id)
        self._schedule_ttl()
        self.save()
        self.notify()
        return results

    def describe(self, lamps: list[str] | None = None) -> dict[str, Any]:
        now = self.now()
        out: dict[str, Any] = {}
        for eid in lamps if lamps is not None else sorted(self.records):
            rec = self.records.get(eid)
            if rec is None:
                continue
            res = resolve(rec, now)
            layers = sorted(rec.layers.values(), key=lambda l: (l.priority, l.seq))
            out[eid] = {
                "active": res.active,
                "effective": res.command.to_json() if res.command else None,
                "base": rec.base.to_json() if rec.base else None,
                "layers": [
                    {**l.to_json(),
                     "set_at": dt_util.utc_from_timestamp(l.set_at).isoformat(),
                     "expires_at": dt_util.utc_from_timestamp(l.expires_at).isoformat()
                     if l.expires_at else None}
                    for l in layers
                ],
                "tombstones": sorted(rec.tombstones),
                "observed": rec.observed.to_json() if rec.observed else None,
                "available": rec.available,
                "diverged": rec.diverged,
                "pending": rec.owed is not None,
                "rendering": self.renderer.alive(eid),
                "untrusted": rec.untrusted,
                "policy": self.policy_for(eid),
                "last_external": (
                    {"at": dt_util.utc_from_timestamp(rec.last_external.at).isoformat(),
                     "source": rec.last_external.source, "policy": rec.last_external.policy,
                     "dropped": list(rec.last_external.dropped)}
                    if rec.last_external else None
                ),
            }
        return out

    # ================================================================ apply switch and status

    async def async_set_apply(self, on: bool) -> None:
        self.apply = on
        if not on:
            # Whatever Layers was still delivering is off: the lamp is unsynced, and no
            # return delivers it later on its own; only layers.sync does (SPEC 7.5).
            for eid, rec in self.records.items():
                if self.renderer.alive(eid) or rec.owed is not None:
                    rec.diverged = DIV_UNSYNCED
                    rec.owed = None
            self.renderer.cancel_all()
        await self._save_now()
        self.notify()

    def _stale(self, rec: Record, now: float) -> bool:
        return not rec.available and rec.observed is not None and (
            now - rec.observed.at > STALE_UNAVAILABLE_S
        )

    def status(self) -> tuple[str, dict[str, Any]]:
        now = self.now()
        failed = sorted(
            e for e in self._failed
            if e in self.records and self.records[e].diverged == DIV_DELIVERY
            and not self._stale(self.records[e], now)
        )
        pending = {
            eid: dt_util.utc_from_timestamp(rec.owed.since).isoformat()
            for eid, rec in self.records.items()
            if rec.owed is not None and not self._stale(rec, now)
        }
        attrs = {"failed": failed, "pending_since": pending,
                 "lamps": len(self.records),
                 "layered": sorted(e for e, r in self.records.items() if r.layers),
                 "untrusted": sorted(e for e, r in self.records.items() if r.untrusted)}
        if not self.apply:
            return STATUS_SHADOW, attrs
        if failed:
            return STATUS_FAILED, attrs
        if pending:
            return STATUS_PENDING, attrs
        return STATUS_OK, attrs
