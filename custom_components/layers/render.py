"""Deliver a lamp its effective command, and verify the lamp actually took it.

Home Assistant drops unavailable entities from a service call without an error,
and a vendor bridge can acknowledge a command the lamp never carries out, or
revert it seconds later. So a command is never fire-and-forget here: each
render is a background task that sends, waits, reads the lamp back, and retries
with backoff until the lamp reports what was asked.

One task per lamp. A newer render, a person taking the lamp back, the apply
switch turning off, and unloading all cancel it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import DOMAIN, EVENT_RENDER, EVENT_RENDER_FAILED, RENDER_CALL_TIMEOUT_S
from .logic.capability import MATCH_COLOUR_OFF, MATCH_YES, matches, observed_from_state
from .logic.model import (
    DIV_COLOUR,
    DIV_DELIVERY,
    LATE_RECHECK_S,
    LATE_WINDOW_S,
    OFF,
    ON,
    RETRY_BACKOFF_S,
    SETTLE_S,
    SLOW_OFF_S,
    SRC_OURS,
    Call,
    Command,
    LastCommand,
    Owed,
)

if TYPE_CHECKING:
    from .engine import Engine

_LOGGER = logging.getLogger(__name__)

SLOW_OFF_PLATFORMS = frozenset({"hue"})      # optimistic off, corrected ~10 s later
INSTANT_OFF_PLATFORMS = frozenset({"matter"})  # turn_off ignores transition


@dataclass(slots=True)
class RenderJob:
    entity_id: str
    target: Command            # the effective command being delivered
    call: Call
    transition: float | None
    parent_id: str | None      # the caller's context id, so the log shows who asked
    layer: str                 # what decided the command ("base" or a layer id)
    owner: str | None
    reason: str                # set / clear / sync / expiry / return / retry / replay


class Renderer:
    """Owns the per-lamp render tasks."""

    def __init__(self, hass: HomeAssistant, engine: Engine) -> None:
        self.hass = hass
        self.engine = engine
        self._tasks: dict[str, asyncio.Task] = {}
        self._jobs: dict[str, RenderJob] = {}
        self._late: dict[str, asyncio.TimerHandle] = {}

    # ------------------------------------------------------------------ control

    def alive(self, entity_id: str) -> bool:
        task = self._tasks.get(entity_id)
        return task is not None and not task.done()

    def job(self, entity_id: str) -> RenderJob | None:
        return self._jobs.get(entity_id) if self.alive(entity_id) else None

    def start(self, job: RenderJob) -> None:
        """Start delivering ``job``; an identical job already running is kept.

        The command is owed (and persisted) from here. The task is registered
        before its first light call runs, not started eagerly: a lamp can write its
        state from inside that call, and while it does the render must already
        count as alive (and be cancellable) for the classifier.
        """
        current = self.job(job.entity_id)
        if current is not None and current.call == job.call:
            return
        self.cancel(job.entity_id)
        rec = self.engine.records[job.entity_id]
        rec.owed = Owed(dt_util.utcnow().timestamp(), job.target, turns_on=job.target.is_on)
        self.engine.save(immediate=True)
        self._jobs[job.entity_id] = job
        self._tasks[job.entity_id] = self.engine.entry.async_create_background_task(
            self.hass, self._run(job), f"{DOMAIN} render {job.entity_id}", eager_start=False
        )
        self.engine.notify()

    def cancel(self, entity_id: str) -> None:
        self._cancel_late(entity_id)
        task = self._tasks.pop(entity_id, None)
        self._jobs.pop(entity_id, None)
        if task is not None and not task.done():
            task.cancel()

    def cancel_all(self) -> None:
        for entity_id in list(self._tasks):
            self.cancel(entity_id)
        for entity_id in list(self._late):
            self._cancel_late(entity_id)

    def in_flight(self) -> dict[str, dict[str, Any]]:
        return {
            eid: {"layer": job.layer, "reason": job.reason, "call": job.call.service}
            for eid, job in self._jobs.items()
            if self.alive(eid)
        }

    # ------------------------------------------------------------------ the task

    async def _run(self, job: RenderJob) -> None:
        eid = job.entity_id
        rec = self.engine.records[eid]
        platform = self.engine.platform(eid)
        transition = job.transition
        attempts = 0
        contexts: list[str] = []
        try:
            for backoff in (0, *RETRY_BACKOFF_S):
                if backoff:
                    await asyncio.sleep(backoff)
                if attempts and self.hass.states.get(eid) is None:
                    # The entity was removed (its integration reloading): away, like
                    # unavailable below. Still owed; its return decides. Without a
                    # state its caps are empty and render_wanted would misread the
                    # dropped brightness as a changed command.
                    _LOGGER.debug("%s: entity gone during render; owed", eid)
                    return
                if attempts and not self.engine.render_wanted(job):
                    # The lamp's command changed without a new render (an expiry that
                    # may not light it, a return recorded as base): stop here.
                    self._abandoned(job)
                    return
                attempts += 1
                now = dt_util.utcnow().timestamp()
                rec.owed = Owed(now, job.target, turns_on=job.target.is_on)
                self.engine.save(immediate=True)
                ctx = Context(parent_id=job.parent_id)
                contexts.append(ctx.id)
                self.engine.add_ours(eid, ctx.id, job.target, now)
                shown = rec.observed
                rec.last_command = LastCommand(
                    now, ours=True, source=SRC_OURS, target=job.target, context_id=ctx.id,
                    from_state=shown.state if shown is not None and shown.state in (ON, OFF) else None,
                )
                self.engine.notify()
                self.hass.bus.async_fire(
                    EVENT_RENDER,
                    {ATTR_ENTITY_ID: eid, "layer": job.layer, "owner": job.owner,
                     "reason": job.reason, "service": job.call.service, "attempt": attempts},
                    context=ctx,
                )
                data: dict[str, Any] = {ATTR_ENTITY_ID: eid, **job.call.as_dict()}
                caps = self.engine.caps(eid)
                if transition and caps.transition and not (
                    job.call.service == "turn_off" and platform in INSTANT_OFF_PLATFORMS
                ):
                    data["transition"] = transition
                try:
                    async with asyncio.timeout(RENDER_CALL_TIMEOUT_S):
                        await self.hass.services.async_call(
                            "light", job.call.service, data, blocking=True, context=ctx
                        )
                except (HomeAssistantError, vol.Invalid, TimeoutError) as err:
                    _LOGGER.debug("%s: %s %s failed (%s); verification decides", eid,
                                  job.call.service, data, err)
                except Exception:  # noqa: BLE001 — whatever the lamp's integration raises
                    _LOGGER.warning("%s: %s %s raised; verification decides", eid,
                                    job.call.service, data, exc_info=True)

                await asyncio.sleep(self._settle(job, data.get("transition"), platform))

                state = self.hass.states.get(eid)
                if state is None or state.state in ("unavailable", "unknown"):
                    # Still owed: the lamp's return will decide (decide_return).
                    _LOGGER.debug("%s: unavailable during render; owed", eid)
                    return
                obs = observed_from_state(state.state, state.attributes,
                                          dt_util.utcnow().timestamp())
                result = matches(obs, job.call, caps)
                if result == MATCH_YES or (result == MATCH_COLOUR_OFF and attempts >= 2):
                    if not self.engine.render_wanted(job):
                        self._abandoned(job)
                        return
                    self._verified(job, colour_off=result == MATCH_COLOUR_OFF, platform=platform)
                    return
                transition = None  # retries go straight there
                _LOGGER.debug("%s: attempt %s not taken (%s)", eid, attempts, result)
            if not self.engine.render_wanted(job):
                self._abandoned(job)
                return
            self._failed(job, attempts)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a render must end in verified, owed or failed
            _LOGGER.exception("%s: render stopped unexpectedly", eid)
            self._failed(job, attempts)
        finally:
            # Every attempt's context stays "ours" until Home Assistant can no longer
            # stamp the lamp's writes with it (5 s after the call; we keep 10 s).
            for context_id in contexts:
                self.engine.release_ours_later(eid, context_id)

    @staticmethod
    def _settle(job: RenderJob, transition: float | None, platform: str) -> float:
        if job.call.service == "turn_off":
            if platform in SLOW_OFF_PLATFORMS:
                return (transition or 0) + SLOW_OFF_S
            if platform in INSTANT_OFF_PLATFORMS:
                return SETTLE_S
        return (transition or 0) + SETTLE_S

    def _verified(self, job: RenderJob, *, colour_off: bool, platform: str) -> None:
        rec = self.engine.records.get(job.entity_id)
        if rec is None:
            return
        rec.owed = None
        rec.diverged = DIV_COLOUR if colour_off else None
        ir.async_delete_issue(self.hass, DOMAIN, f"render_failed_{job.entity_id}")
        self.engine.render_succeeded(job.entity_id)
        self.engine.save()
        if platform in LATE_WINDOW_S:
            self._schedule_late(job)

    def _abandoned(self, job: RenderJob) -> None:
        """The job is no longer the lamp's command: nothing is owed for it."""
        rec = self.engine.records.get(job.entity_id)
        if rec is not None and rec.owed is not None and rec.owed.target == job.target:
            rec.owed = None
        _LOGGER.debug("%s: %s no longer wanted; stopped", job.entity_id, job.call.service)
        self.engine.save()
        self.engine.notify()

    def _failed(self, job: RenderJob, attempts: int) -> None:
        rec = self.engine.records.get(job.entity_id)
        if rec is None:
            return
        rec.owed = None
        rec.diverged = DIV_DELIVERY
        _LOGGER.warning("%s did not take %s after %s attempts", job.entity_id,
                        job.call.service, attempts)
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
        self.engine.render_failed(job.entity_id)
        self.engine.save()

    # ------------------------------------------------------------------ late re-check

    def _schedule_late(self, job: RenderJob) -> None:
        self._cancel_late(job.entity_id)
        self._late[job.entity_id] = self.hass.loop.call_later(
            LATE_RECHECK_S, self._late_check, job
        )

    def _cancel_late(self, entity_id: str) -> None:
        handle = self._late.pop(entity_id, None)
        if handle is not None:
            handle.cancel()

    def _late_check(self, job: RenderJob) -> None:
        """A bridge can revert a verified command half a minute later."""
        self._late.pop(job.entity_id, None)
        if self.alive(job.entity_id):
            return
        self.engine.late_recheck(job.entity_id, job.call)
