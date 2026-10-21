"""Layers on climate entities: the Home Assistant side of ``logic/climate.py``.

The ``Engine`` owns one ``ClimateManager`` and hands it every enrolled
``climate.*`` entity; lamps never reach it and thermostats never reach the lamp
code. What the two share: the ``layers.*`` services, the Apply switch, the store,
the decision trace, the status sensor, our own contexts and the layer sequence.

A command to a thermostat is up to three calls (mode, preset, temperature), sent
under one context. The render then waits for the thermostat to report it, up to
the report-back window, before it retries: a thermostat answers in seconds, not
in the tenths of a second a lamp does. See docs/SPEC.md section 8.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.const import ATTR_ENTITY_ID, EVENT_CALL_SERVICE
from homeassistant.core import CALLBACK_TYPE, Context, Event, EventStateChangedData, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CLIMATE_MANUAL_PRIORITY,
    CONF_CLIMATE_MANUAL_TIMEOUT,
    CONF_CLIMATE_REPORT_WINDOW,
    DOMAIN,
    EVENT_EXTERNAL,
    EVENT_RENDER,
    EVENT_RENDER_FAILED,
    RENDER_CALL_TIMEOUT_S,
    RESULT_IN_SYNC,
    RESULT_PENDING,
    RESULT_QUEUED,
    RESULT_SHADOW,
    RESULT_SKIPPED_NOT_ENROLLED,
    RESULT_UNCHANGED,
    STALE_UNAVAILABLE_S,
)
from .logic import climate as cc
from .logic.model import (
    CALL_MEMORY_S,
    DEBOUNCE_S,
    DIV_DELIVERY,
    DIV_MANUAL_KEEP,
    DIV_UNSYNCED,
    HOLD_CAP,
    HOLD_CAP_WINDOW_S,
    HOLD_SETTLE_S,
    RETURN_SETTLE_S,
    SETTLE_S,
    SRC_AUTOMATION,
    SRC_DEVICE,
    SRC_USER,
)

if TYPE_CHECKING:
    from .engine import Engine

_LOGGER = logging.getLogger(__name__)

CLIMATE_DOMAIN = "climate"
CLIMATE_SERVICES = frozenset({
    "set_temperature", "set_hvac_mode", "set_preset_mode", "turn_on", "turn_off", "toggle",
})
# A render retries twice, spaced out: a thermostat that has not answered inside the
# report-back window is unlikely to answer a burst of re-sends better.
RETRY_BACKOFF_S = (5.0, 30.0)
POLL_S = 0.5                # how often a render looks for the report inside the window
DIV_SYNC_ONLY = frozenset({DIV_MANUAL_KEEP, DIV_UNSYNCED})


@callback
def _climate_call_filter(event_data: dict[str, Any]) -> bool:
    return event_data.get("domain") == CLIMATE_DOMAIN and event_data.get("service") in CLIMATE_SERVICES


def _iso(ts: float | None) -> str | None:
    return dt_util.utc_from_timestamp(ts).isoformat() if ts is not None else None


def _calls_json(calls: tuple[cc.ClimateCall, ...] | None) -> list[dict[str, Any]] | None:
    if calls is None:
        return None
    return [{"service": f"{CLIMATE_DOMAIN}.{c.service}", **c.as_dict()} for c in calls]


@dataclass(slots=True)
class ClimateJob:
    entity_id: str
    target: cc.ClimateCommand
    calls: tuple[cc.ClimateCall, ...]
    parent_id: str | None
    layer: str
    owner: str | None
    reason: str


class ClimateManager:
    """Everything Layers knows and does for the enrolled climate entities."""

    def __init__(self, engine: Engine, entity_ids: set[str], options: dict[str, Any]) -> None:
        self.engine = engine
        self.hass = engine.hass
        self.enrolled = set(entity_ids)
        self.records: dict[str, cc.ClimateRecord] = {}
        self.manual_priority = int(options.get(CONF_CLIMATE_MANUAL_PRIORITY,
                                               cc.DEFAULT_MANUAL_PRIORITY))
        minutes = options.get(CONF_CLIMATE_MANUAL_TIMEOUT, cc.DEFAULT_MANUAL_TIMEOUT_S / 60)
        self.manual_timeout: float | None = float(minutes) * 60 if minutes else None
        self.window = float(options.get(CONF_CLIMATE_REPORT_WINDOW, cc.DEFAULT_REPORT_WINDOW_S))
        self.renderer = ClimateRenderer(self)
        self._platforms: dict[str, str] = {}
        self._calls: dict[str, tuple[str, str | None, float]] = {}
        self._timers: dict[tuple[str, str], CALLBACK_TYPE] = {}
        self._ttl_unsub: CALLBACK_TYPE | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._failed: set[str] = set()
        self._late_retried: set[str] = set()
        self._reshows: dict[str, list[float]] = {}

    # ================================================================ lifecycle

    def load(self, stored: dict[str, Any], now: float) -> None:
        registry = er.async_get(self.hass)
        for eid in sorted(self.enrolled):
            rec = cc.ClimateRecord.from_json(eid, stored[eid]) if eid in stored else cc.ClimateRecord(eid)
            stored_obs = rec.observed
            current = self._obs(self.hass.states.get(eid), now)
            rec.observed = current
            rec.available = bool(current and current.available)
            if rec.p_at_drop is None and stored_obs is not None and stored_obs.available:
                rec.p_at_drop = stored_obs      # downtime counts as time away, as for lamps
            self.records[eid] = rec
            entry = registry.async_get(eid)
            self._platforms[eid] = entry.platform if entry else ""

    def start(self) -> None:
        if self.enrolled:
            self._unsubs.append(
                async_track_state_change_event(self.hass, sorted(self.enrolled), self._on_state)
            )
            self._unsubs.append(
                self.hass.bus.async_listen(EVENT_CALL_SERVICE, self._on_call,
                                           event_filter=_climate_call_filter)
            )

    def cancel_all(self) -> None:
        for unsub in self._timers.values():
            unsub()
        self._timers.clear()
        if self._ttl_unsub:
            self._ttl_unsub()
            self._ttl_unsub = None
        self.renderer.cancel_all()

    def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self.cancel_all()

    def data_to_save(self) -> dict[str, Any]:
        return {eid: rec.to_json() for eid, rec in self.records.items() if rec.worth_persisting()}

    def rename(self, old: str, new: str) -> None:
        rec = self.records.pop(old, None)
        if rec is not None:
            rec.entity_id = new
            self.records[new] = rec
        self.renderer.cancel(old)
        self.enrolled.discard(old)
        self.enrolled.add(new)

    # ================================================================ helpers

    def now(self) -> float:
        return self.engine.now()

    @staticmethod
    def _obs(state: State | None, now: float | None = None) -> cc.ClimateObserved | None:
        if state is None:
            return None
        return cc.observed_from_state(state.state, state.attributes,
                                      now or state.last_updated_timestamp)

    def caps(self, eid: str) -> cc.ClimateCaps:
        state = self.hass.states.get(eid)
        return cc.caps_from_attrs(state.attributes if state else {}, self._platforms.get(eid, ""))

    def _decision(self, eid: str, kind: str, **extra: Any) -> None:
        self.engine._decision(eid, kind, domain=CLIMATE_DOMAIN, **extra)  # noqa: SLF001

    def _later(self, kind: str, eid: str, delay: float, action: Callable[[str], None]) -> None:
        key = (kind, eid)
        self._cancel(kind, eid)

        @callback
        def _fire(_now: Any) -> None:
            self._timers.pop(key, None)
            try:
                action(eid)
            except Exception:  # noqa: BLE001 — a timer must never take the engine down
                _LOGGER.exception("layers: climate %s timer for %s failed", kind, eid)
                self._mark_untrusted(eid)

        self._timers[key] = async_call_later(self.hass, delay, _fire)

    def _cancel(self, kind: str, eid: str) -> None:
        unsub = self._timers.pop((kind, eid), None)
        if unsub:
            unsub()

    def _pending(self, kind: str, eid: str) -> bool:
        return (kind, eid) in self._timers

    def _mark_untrusted(self, eid: str) -> None:
        rec = self.records.get(eid)
        if rec is not None:
            rec.untrusted = True
            self.renderer.cancel(eid)

    def _runtime(self, eid: str) -> cc.ClimateRuntime:
        now = self.now()
        for key in [k for k, (_s, _u, at) in self._calls.items() if now - at > CALL_MEMORY_S]:
            del self._calls[key]
        ours = self.engine._ours.get(eid, {})  # noqa: SLF001 — shared with the lamps
        return cc.ClimateRuntime(
            ours={cid: oc.target for cid, oc in ours.items()},
            calls={cid: (src, user) for cid, (src, user, _at) in self._calls.items()},
            render_alive=self.renderer.alive(eid),
            returning=self._pending("return", eid),
            window=self.window,
        )

    # ================================================================ other integrations' calls

    @callback
    def _on_call(self, event: Event) -> None:
        """Remember who made a climate call, so the state change it causes is attributed.
        Home Assistant stamps it with the call's context for 5 s; a call from an
        integration carries neither a user nor a parent, which says nothing else."""
        ctx = event.context
        if self.engine._is_ours(ctx.id):  # noqa: SLF001
            return
        self._calls[ctx.id] = (SRC_USER if ctx.user_id else SRC_AUTOMATION, ctx.user_id, self.now())

    # ================================================================ state events

    @callback
    def _on_state(self, event: Event[EventStateChangedData]) -> None:
        rec = self.records.get(event.data["entity_id"])
        if rec is None:
            return
        try:
            self._handle_state(rec, event)
        except Exception:  # noqa: BLE001 — one thermostat's bad event must not stop the rest
            _LOGGER.exception("layers: could not classify a change on %s", rec.entity_id)
            self._mark_untrusted(rec.entity_id)

    def _handle_state(self, rec: cc.ClimateRecord, event: Event[EventStateChangedData]) -> None:
        eid = rec.entity_id
        now = self.now()
        old_state, new_state = event.data["old_state"], event.data["new_state"]
        old, new = self._obs(old_state, now), self._obs(new_state, now)
        ctx = new_state.context if new_state is not None else event.context
        caps = self.caps(eid)
        ev = cc.ClimateEvent(eid, old, new, ctx.id, ctx.parent_id, ctx.user_id, now)
        verdict = cc.classify(rec, ev, self._runtime(eid), caps, now)
        kind = verdict.kind
        if kind not in (cc.MEASUREMENT, cc.IGNORE):
            # Room-temperature reports arrive every few seconds to minutes: they would
            # push everything else out of the decision trace.
            self._decision(eid, kind, source=verdict.source)

        if kind == cc.IGNORE:
            return
        if kind in (cc.TRANSPORT_DOWN, cc.GONE):
            if rec.observed is not None and rec.observed.available and rec.p_at_drop is None:
                rec.p_at_drop = rec.observed
            rec.available = False
            rec.observed = new
            self._cancel("debounce", eid)
            self._cancel("window", eid)
            rec.observed_prev = None
            self._cancel("return", eid)
            self.engine.save()
            self.engine.notify()
            return
        if kind == cc.FIRST and (self.engine.in_grace or rec.available):
            rec.observed = new
            rec.available = bool(new and new.available)
            return
        if kind in (cc.TRANSPORT_UP, cc.FIRST):
            rec.available = True
            rec.observed = new
            if not self.engine.in_grace:
                self._later("return", eid, RETURN_SETTLE_S, self._judge_return)
            self.engine.notify()
            return

        rec.observed = new
        last = rec.last_command
        if last is not None and last.ours:
            if not cc.shows_target(new, last.target, caps):
                last.matched_at = None
            elif last.matched_at is None:
                last.matched_at = now
        if kind in (cc.MEASUREMENT, cc.OURS):
            self.engine.save(lazy=True)
            return
        if kind == cc.EXTERNAL:
            self._external(rec, verdict.source or SRC_DEVICE, now, verdict.user_id)
            return
        if self._pending("return", eid) or self.engine.in_grace:
            self.engine.save(lazy=True)
            return
        if kind == cc.NOISE:
            self.engine.save(lazy=True)
            return
        if kind == cc.LATE:
            if not self._pending("window", eid) and last is not None:
                self._later("window", eid, max(0.0, last.at + self.window - now), self._window_end)
            self.engine.save(lazy=True)
            return
        if kind == cc.DEBOUNCE:
            if not self._pending("debounce", eid):
                rec.observed_prev = old
            self._later("debounce", eid, DEBOUNCE_S, self._settle_debounce)

    @callback
    def _settle_debounce(self, eid: str) -> None:
        rec = self.records[eid]
        current = rec.observed
        if current is None or not current.available or self.engine.in_grace:
            rec.observed_prev = None
            return
        verdict = cc.settle(rec, current, self.caps(eid), self.now())
        self._decision(eid, verdict.kind, source=verdict.source, settled=True)
        if verdict.kind == cc.EXTERNAL:
            self._external(rec, SRC_DEVICE, self.now())
        rec.observed_prev = None

    def _flush_debounce(self, eid: str) -> None:
        if self._pending("debounce", eid):
            self._cancel("debounce", eid)
            self._settle_debounce(eid)

    @callback
    def _window_end(self, eid: str) -> None:
        """The report-back window after our command is over, and a report with no
        context came inside it. Still not showing the command: one more try, then
        it counts as failed. (A person's change inside the window is the price of
        a slow device: it is sent over once.)"""
        rec = self.records[eid]
        if self.renderer.alive(eid) or not rec.available:
            return
        caps = self.caps(eid)
        effective = cc.resolve(rec, self.now())[0]
        if cc.shows(rec.observed, effective, caps):
            self._decision(eid, "window_ok")
            return
        if eid in self._late_retried:
            self._decision(eid, "late_retry_spent")
            rec.diverged = DIV_DELIVERY
            self._failed.add(eid)
        else:
            self._late_retried.add(eid)
            self._decision(eid, "window_failed")
            self._render(eid, reason="retry", parent_id=None, deferred=True)
        self.engine.save()
        self.engine.notify()

    def _supersede(self, rec: cc.ClimateRecord) -> None:
        eid = rec.entity_id
        for kind in ("debounce", "return", "window"):
            self._cancel(kind, eid)
        rec.observed_prev = None
        if rec.available:
            rec.p_at_drop = None
        self.renderer.cancel(eid)

    def _external(self, rec: cc.ClimateRecord, source: str, now: float,
                  user_id: str | None = None) -> None:
        """A change Layers did not make: it becomes the manual layer (SPEC 8.5)."""
        eid = rec.entity_id
        self._supersede(rec)
        shown = cc.observed_to_command(rec.observed)
        if shown is None:
            return
        scope = cc.change_scope(source)
        result = cc.apply_external(
            rec, shown, source, now, self.engine.next_seq,
            manual_priority=self.manual_priority, manual_timeout=self.manual_timeout,
            caps=self.caps(eid), user_id=user_id, scope=scope,
        )
        self._decision(eid, "manual", source=source, command=shown.to_json(),
                       dropped=list(result.dropped), held=list(result.held))
        self._failed.discard(eid)
        self.hass.bus.async_fire(
            EVENT_EXTERNAL,
            {ATTR_ENTITY_ID: eid, "source": source, "policy": cc.POLICY_MANUAL_LAYER,
             "scope": scope, "dropped": list(result.dropped), "edited": result.edited,
             "held": list(result.held)},
        )
        if result.held:
            self._decision(eid, "hold", scope=scope)
            self._later("hold", eid, HOLD_SETTLE_S, self._reshow)
        self.schedule_ttl()
        self.engine.save()
        self.engine.notify()

    @callback
    def _reshow(self, eid: str) -> None:
        rec = self.records.get(eid)
        if rec is None or not rec.available:
            return
        now = self.now()
        recent = [t for t in self._reshows.get(eid, ()) if now - t < HOLD_CAP_WINDOW_S]
        if len(recent) >= HOLD_CAP:
            self._reshows[eid] = recent
            self._decision(eid, "hold_capped", reshows=len(recent))
            rec.diverged = DIV_DELIVERY
            self._failed.add(eid)
            self.engine.save()
            self.engine.notify()
            return
        self._flush_debounce(eid)
        if self._render(eid, reason="hold", parent_id=None, deferred=True) == RESULT_QUEUED:
            recent.append(now)
        self._reshows[eid] = recent
        self.engine.save()

    # ================================================================ returns and startup

    @callback
    def _judge_return(self, eid: str) -> None:
        rec = self.records[eid]
        if rec.observed is None or not rec.available:
            return
        self._return_decision(rec, self.now(), reason="return")
        rec.p_at_drop = None
        self.engine.save()
        self.engine.notify()

    def _return_decision(self, rec: cc.ClimateRecord, now: float, reason: str) -> None:
        eid = rec.entity_id
        kind = cc.decide_return(rec, rec.observed, self.caps(eid), now)
        self._decision(eid, f"return:{kind}", reason=reason)
        if kind == cc.SEND:
            self._render(eid, reason=reason, parent_id=None, deferred=True)
        elif kind == cc.RECORD:
            rec.base = cc.observed_to_command(rec.observed)
            rec.base_source, rec.base_at = reason, now
            rec.owed = None
            self._drop_stale_render(eid)
        elif kind == cc.NOTHING:
            rec.owed = None
            self._drop_stale_render(eid)
        elif kind == cc.EXTERNAL:
            rec.owed = None
            self._external(rec, SRC_DEVICE, now)

    def end_grace(self) -> None:
        now = self.now()
        for eid, rec in list(self.records.items()):
            try:
                self._end_grace_one(rec, now)
            except Exception:  # noqa: BLE001 — one thermostat must not stop the others
                _LOGGER.exception("layers: could not reconcile %s at the end of the grace", eid)
                self._mark_untrusted(eid)
        self.schedule_ttl()

    def _end_grace_one(self, rec: cc.ClimateRecord, now: float) -> None:
        eid = rec.entity_id
        caps = self.caps(eid)
        if self._has_expired(rec, now):
            shown = rec.observed if rec.available else rec.p_at_drop
            res = cc.expire(rec, now, shown)
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
        if not rec.available:
            return      # judged by decide_return when it comes back
        if self.renderer.alive(eid):
            rec.p_at_drop = None
            return
        if rec.owed is not None and rec.observed is not None:
            self._return_decision(rec, now, reason="startup")
        elif not rec.layers:
            if rec.observed is not None:
                rec.base = cc.observed_to_command(rec.observed)
                rec.base_source, rec.base_at = "startup", now
                rec.diverged = None
        else:
            effective = cc.resolve(rec, now)[0]
            if not cc.shows(rec.observed, effective, caps):
                before = rec.p_at_drop
                if before is not None and before.available and not cc.close(rec.observed, before, caps):
                    self._decision(eid, "startup:external")
                    self._external(rec, SRC_DEVICE, now)
                    return
                rec.diverged = DIV_UNSYNCED
        rec.p_at_drop = None

    # ================================================================ rendering

    def _render(self, eid: str, *, reason: str, parent_id: str | None,
                deferred: bool = False) -> str:
        """Hand the thermostat its effective command. The ONLY place a climate command starts."""
        rec = self.records[eid]
        if deferred and (rec.untrusted or rec.diverged in DIV_SYNC_ONLY):
            self.renderer.cancel(eid)
            rec.owed = None
            return RESULT_UNCHANGED
        now = self.now()
        effective, active = cc.resolve(rec, now)
        caps = self.caps(eid)
        calls = cc.project(effective, caps)
        if not calls:
            self.renderer.cancel(eid)
            rec.owed = None
            self.engine.notify()
            return RESULT_UNCHANGED
        job = self.renderer.job(eid)
        if (rec.available and cc.matches(rec.observed, calls, caps)
                and (job is None or job.calls == calls)):
            self.renderer.cancel(eid)
            rec.owed = None
            rec.diverged = None
            rec.would_send = None
            self.engine.notify()
            return RESULT_IN_SYNC
        if not self.engine.apply:
            # Shadow: record what would have been sent, and send nothing (SPEC 7.5).
            self.renderer.cancel(eid)
            rec.diverged = DIV_UNSYNCED
            rec.owed = None
            rec.would_send = calls
            self._decision(eid, "shadow", reason=reason, layer=active, calls=_calls_json(calls))
            self.engine.notify()
            return RESULT_SHADOW
        rec.would_send = None
        if not deferred and rec.diverged in DIV_SYNC_ONLY:
            rec.diverged = None
        if not rec.available:
            self.renderer.cancel(eid)
            rec.owed = cc.ClimateOwed(now, effective)
            self.engine.notify()
            return RESULT_PENDING
        if reason != "retry":
            self._late_retried.discard(eid)
        self._cancel("window", eid)
        layer = rec.layers.get(active)
        self.renderer.start(ClimateJob(eid, effective, calls, parent_id, active,
                                       layer.owner if layer else None, reason))
        self._decision(eid, "render", reason=reason, layer=active, calls=_calls_json(calls))
        return RESULT_QUEUED

    def render_wanted(self, job: ClimateJob) -> bool:
        rec = self.records.get(job.entity_id)
        if rec is None or not self.engine.apply:
            return False
        return cc.project(cc.resolve(rec, self.now())[0], self.caps(job.entity_id)) == job.calls

    def _drop_stale_render(self, eid: str) -> None:
        job = self.renderer.job(eid)
        if job is None or self.render_wanted(job):
            return
        self.renderer.cancel(eid)
        rec = self.records[eid]
        if rec.owed is not None and not rec.owed.missed:
            rec.owed = None

    # ================================================================ TTL

    @staticmethod
    def _has_expired(rec: cc.ClimateRecord, now: float) -> bool:
        return (any(not lay.live(now) for lay in rec.layers.values())
                or any(not t.live(now) for t in rec.tombstones.values()))

    def schedule_ttl(self) -> None:
        if self._ttl_unsub:
            self._ttl_unsub()
            self._ttl_unsub = None
        if self.engine.in_grace or self.engine._stopping:  # noqa: SLF001
            return
        times = [t for rec in self.records.values() if not rec.untrusted
                 for t in [*(lay.expires_at for lay in rec.layers.values()),
                           *(s.expires_at for s in rec.tombstones.values())]
                 if t is not None]
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
                    self._flush_debounce(eid)
                    shown = rec.observed if rec.available else rec.p_at_drop
                    res = cc.expire(rec, now, shown)
                    if res.expired or res.lifted:
                        self._decision(eid, "expiry", expired=list(res.expired), render=res.render)
                    if res.render:
                        self._render(eid, reason="expiry", parent_id=None, deferred=True)
                    else:
                        self._drop_stale_render(eid)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("layers: could not expire layers on %s", eid)
                    self._mark_untrusted(eid)
        finally:
            self.schedule_ttl()
            self.engine.save()
            self.engine.notify()

    # ================================================================ services

    def validate_set(self, entity_ids: list[str], req: cc.ClimateSetRequest) -> None:
        """Raise PolicyError before touching anything if one thermostat would refuse."""
        now = self.now()
        for eid in entity_ids:
            rec = self.records.get(eid)
            if rec is None:
                continue
            mode = req.command.hvac_mode
            caps = self.caps(eid)
            if mode is not None and caps.hvac_modes and mode not in caps.hvac_modes:
                raise cc.PolicyError(cc.ERR_INVALID_REQUEST, entity_id=eid, layer=req.layer,
                                     reason=f"{eid} has no hvac_mode {mode!r}")
            preset = req.command.preset
            if preset is not None and not (caps.presets and (not caps.preset_modes
                                                             or preset in caps.preset_modes)):
                raise cc.PolicyError(cc.ERR_INVALID_REQUEST, entity_id=eid, layer=req.layer,
                                     reason=f"{eid} has no preset {preset!r}")
            probe = cc.ClimateRecord.from_json(eid, rec.to_json())
            cc.apply_set(probe, req, now, lambda: 0, self.manual_priority)

    def _learn_base(self, rec: cc.ClimateRecord, now: float) -> None:
        if rec.base is None and not rec.layers and rec.available and rec.observed is not None:
            rec.base = cc.observed_to_command(rec.observed)
            rec.base_source, rec.base_at = "observed", now

    def set_layer(self, entity_ids: list[str], req: cc.ClimateSetRequest,
                  parent_id: str | None) -> dict[str, str]:
        now = self.now()
        results: dict[str, str] = {}
        for eid in entity_ids:
            rec = self.records.get(eid)
            if rec is None:
                results[eid] = RESULT_SKIPPED_NOT_ENROLLED
                continue
            self._flush_debounce(eid)
            self._learn_base(rec, now)
            before = cc.resolve(rec, now)[0]
            outcome = cc.apply_set(rec, req, now, self.engine.next_seq, self.manual_priority)
            self._decision(eid, f"set:{outcome.result}", layer=outcome.layer)
            if outcome.result.startswith("skipped"):
                results[eid] = outcome.result
                continue
            if cc.resolve(rec, now)[0] != before or rec.diverged == DIV_DELIVERY:
                results[eid] = self._render(eid, reason="set", parent_id=parent_id)
            else:
                results[eid] = RESULT_UNCHANGED
        self.schedule_ttl()
        return results

    def clear_layer(self, entity_ids: list[str] | None, layer: str,
                    parent_id: str | None) -> dict[str, str]:
        now = self.now()
        if entity_ids is None:
            entity_ids = [e for e, r in self.records.items()
                          if layer in r.layers or layer in r.tombstones]
        results: dict[str, str] = {}
        for eid in entity_ids:
            rec = self.records.get(eid)
            if rec is None:
                results[eid] = RESULT_SKIPPED_NOT_ENROLLED
                continue
            self._flush_debounce(eid)
            before = cc.resolve(rec, now)[0]
            cleared = cc.apply_clear(rec, layer, now)
            self._decision(eid, "clear", layer=layer, removed=list(cleared.removed),
                           kept=list(cleared.kept))
            if cc.resolve(rec, now)[0] != before or rec.diverged == DIV_DELIVERY:
                results[eid] = self._render(eid, reason="clear", parent_id=parent_id)
            else:
                results[eid] = RESULT_UNCHANGED
        self.schedule_ttl()
        return results

    def sync(self, entity_ids: list[str] | None, parent_id: str | None) -> dict[str, str]:
        results: dict[str, str] = {}
        for eid in entity_ids if entity_ids is not None else sorted(self.records):
            rec = self.records.get(eid)
            if rec is None:
                results[eid] = RESULT_SKIPPED_NOT_ENROLLED
                continue
            self._flush_debounce(eid)
            rec.untrusted = False
            if rec.diverged in DIV_SYNC_ONLY:
                rec.diverged = None
            results[eid] = self._render(eid, reason="sync", parent_id=parent_id)
        self.schedule_ttl()
        return results

    def describe(self, entity_ids: list[str] | None = None) -> dict[str, Any]:
        now = self.now()
        out: dict[str, Any] = {}
        for eid in entity_ids if entity_ids is not None else sorted(self.records):
            rec = self.records.get(eid)
            if rec is None:
                continue
            effective, active = cc.resolve(rec, now)
            out[eid] = {
                "domain": CLIMATE_DOMAIN,
                "active": active,
                "effective": effective.to_json() if effective else None,
                "base": rec.base.to_json() if rec.base else None,
                "layers": [
                    {**lay.to_json(), "set_at": _iso(lay.set_at), "expires_at": _iso(lay.expires_at)}
                    for lay in sorted(rec.layers.values(), key=lambda lay: (lay.priority, lay.seq))
                ],
                "tombstones": sorted(rec.tombstones),
                "observed": rec.observed.to_json() if rec.observed else None,
                "available": rec.available,
                "diverged": rec.diverged,
                "pending": rec.owed is not None,
                "rendering": self.renderer.alive(eid),
                "untrusted": rec.untrusted,
                "policy": cc.POLICY_MANUAL_LAYER,
                "manual_priority": self.manual_priority,
                "would_send": _calls_json(rec.would_send),
                "last_external": (
                    {"at": _iso(rec.last_external.at), "source": rec.last_external.source,
                     "policy": rec.last_external.policy,
                     "dropped": list(rec.last_external.dropped)}
                    if rec.last_external else None
                ),
            }
        return out

    # ================================================================ apply and status

    def on_apply(self, on: bool) -> None:
        if on:
            return
        for eid, rec in self.records.items():
            if self.renderer.alive(eid) or rec.owed is not None:
                rec.diverged = DIV_UNSYNCED
                rec.owed = None
        self.renderer.cancel_all()
        for eid in list(self.records):
            self._cancel("window", eid)

    def render_succeeded(self, eid: str) -> None:
        self._failed.discard(eid)
        self.engine.notify()

    def render_failed(self, eid: str) -> None:
        self._failed.add(eid)
        self.engine.notify()

    def _stale(self, rec: cc.ClimateRecord, now: float) -> bool:
        return not rec.available and rec.observed is not None and (
            now - rec.observed.at > STALE_UNAVAILABLE_S)

    def status(self) -> dict[str, Any]:
        now = self.now()
        return {
            "failed": sorted(e for e in self._failed if e in self.records
                             and self.records[e].diverged == DIV_DELIVERY
                             and not self._stale(self.records[e], now)),
            "pending_since": {eid: _iso(rec.owed.since) for eid, rec in self.records.items()
                              if rec.owed is not None and not self._stale(rec, now)},
            "layered": sorted(e for e, r in self.records.items() if r.layers),
            "untrusted": sorted(e for e, r in self.records.items() if r.untrusted),
        }


class ClimateRenderer:
    """One task per thermostat: send, wait for the report inside the window, retry."""

    def __init__(self, manager: ClimateManager) -> None:
        self.manager = manager
        self.hass = manager.hass
        self._tasks: dict[str, asyncio.Task] = {}
        self._jobs: dict[str, ClimateJob] = {}

    def alive(self, eid: str) -> bool:
        task = self._tasks.get(eid)
        return task is not None and not task.done()

    def job(self, eid: str) -> ClimateJob | None:
        return self._jobs.get(eid) if self.alive(eid) else None

    def start(self, job: ClimateJob) -> None:
        current = self.job(job.entity_id)
        if current is not None and current.calls == job.calls:
            return
        self.cancel(job.entity_id)
        engine = self.manager.engine
        self.manager.records[job.entity_id].owed = cc.ClimateOwed(engine.now(), job.target)
        engine.save(immediate=True)
        self._jobs[job.entity_id] = job
        self._tasks[job.entity_id] = engine.entry.async_create_background_task(
            self.hass, self._run(job), f"{DOMAIN} climate render {job.entity_id}",
            eager_start=False,
        )
        engine.notify()

    def cancel(self, eid: str) -> None:
        task = self._tasks.pop(eid, None)
        self._jobs.pop(eid, None)
        if task is not None and not task.done():
            task.cancel()

    def cancel_all(self) -> None:
        for eid in list(self._tasks):
            self.cancel(eid)

    def in_flight(self) -> dict[str, dict[str, Any]]:
        return {eid: {"layer": job.layer, "reason": job.reason,
                      "call": ",".join(c.service for c in job.calls)}
                for eid, job in self._jobs.items() if self.alive(eid)}

    def _current(self, eid: str) -> cc.ClimateObserved | None:
        return ClimateManager._obs(self.hass.states.get(eid), self.manager.now())

    async def _run(self, job: ClimateJob) -> None:
        eid = job.entity_id
        manager, engine = self.manager, self.manager.engine
        rec = manager.records[eid]
        attempts = 0
        contexts: list[str] = []
        loop = self.hass.loop
        try:
            for backoff in (0.0, *RETRY_BACKOFF_S):
                if backoff:
                    await asyncio.sleep(backoff)
                if attempts and self.hass.states.get(eid) is None:
                    return          # removed: still owed, its return decides
                if attempts and not manager.render_wanted(job):
                    self._abandoned(job)
                    return
                attempts += 1
                now = engine.now()
                rec.owed = cc.ClimateOwed(now, job.target)
                engine.save(immediate=True)
                ctx = Context(parent_id=job.parent_id)
                contexts.append(ctx.id)
                engine.add_ours(eid, ctx.id, job.target, now)
                rec.last_command = cc.ClimateLastCommand(now, True, job.target, ctx.id)
                engine.notify()
                self.hass.bus.async_fire(
                    EVENT_RENDER,
                    {ATTR_ENTITY_ID: eid, "layer": job.layer, "owner": job.owner,
                     "reason": job.reason, "service": ",".join(c.service for c in job.calls),
                     "attempt": attempts},
                    context=ctx,
                )
                caps = manager.caps(eid)
                for call in job.calls:
                    if cc.call_shown(self._current(eid), call, caps):
                        continue        # already shows it: one write fewer to a slow device
                    try:
                        async with asyncio.timeout(RENDER_CALL_TIMEOUT_S):
                            await self.hass.services.async_call(
                                CLIMATE_DOMAIN, call.service,
                                {ATTR_ENTITY_ID: eid, **call.as_dict()},
                                blocking=True, context=ctx,
                            )
                    except (HomeAssistantError, vol.Invalid, TimeoutError) as err:
                        _LOGGER.debug("%s: %s failed (%s); verification decides", eid,
                                      call.service, err)
                    except Exception:  # noqa: BLE001 — whatever the integration raises
                        _LOGGER.warning("%s: %s raised; verification decides", eid,
                                        call.service, exc_info=True)

                # Wait for the report: at least SETTLE_S, at most the report-back window.
                deadline = loop.time() + max(manager.window, SETTLE_S)
                await asyncio.sleep(SETTLE_S)
                while True:
                    obs = self._current(eid)
                    if obs is None or not obs.available:
                        return      # still owed: its return decides
                    if cc.matches(obs, job.calls, caps):
                        if not manager.render_wanted(job):
                            self._abandoned(job)
                        else:
                            self._verified(job)
                        return
                    if loop.time() >= deadline:
                        break
                    await asyncio.sleep(POLL_S)
                _LOGGER.debug("%s: attempt %s not reported inside the window", eid, attempts)
            if not manager.render_wanted(job):
                self._abandoned(job)
                return
            self._failed(job, attempts)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a render must end in verified, owed or failed
            _LOGGER.exception("%s: climate render stopped unexpectedly", eid)
            self._failed(job, attempts)
        finally:
            for context_id in contexts:
                engine.release_ours_later(eid, context_id)

    def _verified(self, job: ClimateJob) -> None:
        rec = self.manager.records.get(job.entity_id)
        if rec is None:
            return
        rec.owed = None
        rec.diverged = None
        ir.async_delete_issue(self.hass, DOMAIN, f"render_failed_{job.entity_id}")
        self.manager._decision(job.entity_id, "verified", reason=job.reason)  # noqa: SLF001
        self.manager.render_succeeded(job.entity_id)
        self.manager.engine.save()

    def _abandoned(self, job: ClimateJob) -> None:
        rec = self.manager.records.get(job.entity_id)
        if rec is not None and rec.owed is not None and rec.owed.target == job.target:
            rec.owed = None
        self.manager.engine.save()
        self.manager.engine.notify()

    def _failed(self, job: ClimateJob, attempts: int) -> None:
        rec = self.manager.records.get(job.entity_id)
        if rec is None:
            return
        rec.owed = None
        rec.diverged = DIV_DELIVERY
        _LOGGER.warning("%s did not report %s after %s attempts", job.entity_id,
                        [c.service for c in job.calls], attempts)
        self.manager._decision(job.entity_id, "render_failed", attempts=attempts)  # noqa: SLF001
        ir.async_create_issue(
            self.hass, DOMAIN, f"render_failed_{job.entity_id}",
            is_fixable=False, is_persistent=False, severity=ir.IssueSeverity.WARNING,
            translation_key="render_failed",
            translation_placeholders={"entity_id": job.entity_id, "attempts": str(attempts)},
        )
        self.hass.bus.async_fire(
            EVENT_RENDER_FAILED,
            {ATTR_ENTITY_ID: job.entity_id, "layer": job.layer, "attempts": attempts},
        )
        self.manager.render_failed(job.entity_id)
        self.manager.engine.save()
