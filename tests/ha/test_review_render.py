"""Regression tests: the paths that send a lamp a command (SPEC 7.2, 7.3).

Each test names the review finding it pins down. They use the ticking clock and
helpers of test_behaviour.py; ``RETRY_BACKOFF_S`` is monkeypatched where a render
has to stay in flight ("parked") while something else happens.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from freezegun.api import TickingDateTimeFactory
from homeassistant.core import Context, Event, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockUser, async_capture_events

from custom_components.layers import render as render_module
from custom_components.layers.const import DOMAIN, EVENT_RENDER, EVENT_RENDER_FAILED
from custom_components.layers.logic import policy as policy_module
from custom_components.layers.logic.model import DIV_MANUAL_KEEP, DIV_UNSYNCED, OFF_COMMAND, Command

from .conftest import FakeLamp, settle
from .test_behaviour import (
    A,
    B,
    C,
    advance,
    brightness,
    kinds,
    layers,
    light,
    sent_by_layers,
    start,
    state,
)

pytestmark = pytest.mark.freeze_time(tick=True)

STATUS = "sensor.layers_status"
PARKED = (30.0,)          # one retry, 30 s after the first attempt


@pytest.fixture
def renders(hass: HomeAssistant) -> list[Event]:
    return async_capture_events(hass, EVENT_RENDER)


@pytest.fixture
def person(hass_admin_user: MockUser) -> Context:
    return Context(user_id=hass_admin_user.id)


async def call(hass: HomeAssistant, service: str, **data: Any) -> dict[str, str]:
    """A layers.* call that lets nothing else run before it returns."""
    response = await hass.services.async_call(DOMAIN, service, data, blocking=True,
                                              return_response=True)
    return response["entities"]


async def let_run(times: int = 5) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


async def tick(hass: HomeAssistant, freezer: Any, seconds: float) -> None:
    """Move the clock and run what fell due, without waiting for renders to finish
    (``advance`` waits for background tasks, and a parked render never finishes)."""
    freezer.tick(timedelta(seconds=seconds))
    await let_run()
    await hass.async_block_till_done()
    await asyncio.sleep(0.05)          # a render's short real-time settle
    await hass.async_block_till_done()


# --------------------------------------------------------------------------- R1-1


async def test_a_clear_right_after_a_set_does_not_trust_the_lamps_stale_report(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
) -> None:
    """Review 1 #1: the lamp takes our ON at once but reports it 0.3 s later (Hue,
    Matter, Zigbee). A clear before that report must not read the stale "off" as in
    sync and cancel: our ON would land after and nothing would ever correct it."""
    engine = await start(hass, [C])
    lamp = lights["c"]
    lamp.report_after = 0.3
    assert await call(hass, "set", entity_id=C, layer="sig", priority=70,
                      brightness=255) == {C: "queued"}
    await let_run()
    assert lamp.calls[-1][0] == "turn_on"          # our ON went out; the report is to come
    assert state(hass, C).state == "off"

    assert await call(hass, "clear", entity_id=C, layer="sig") == {C: "queued"}
    await settle(hass, 0.6)
    assert state(hass, C).state == "off"
    rec = engine.records[C]
    assert (rec.owed, rec.diverged) == (None, None)
    assert [s for s, _ in sent_by_layers(lamp, renders)] == ["turn_on", "turn_off"]


# --------------------------------------------------------------------------- R1-3 / R2-1


async def test_a_clear_that_leaves_nothing_to_send_stops_the_render_in_flight(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    freezer: TickingDateTimeFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 1 #3 / review 2 #1 A: a lamp away at startup has no base. Its layer is
    delivered on return, the lamp ignores it, and the owner clears the layer: the
    effective command is None, and no retry may send the layer after that."""
    lamp = lights["c"]
    lamp.set_available(False)
    await hass.async_block_till_done()
    engine = await start(hass, [C])
    rec = engine.records[C]
    assert rec.base is None
    monkeypatch.setattr(render_module, "RETRY_BACKOFF_S", PARKED)
    assert await layers(hass, "set", entity_id=C, layer="night", priority=50,
                        brightness=13) == {C: "pending"}
    lamp.ignore_commands = True
    lamp.set_available(True)
    await hass.async_block_till_done()
    await tick(hass, freezer, 5.5)                  # return:send, attempt 1 ignored, parked
    assert engine.renderer.alive(C) and len(lamp.calls) == 1

    assert await layers(hass, "clear", entity_id=C, layer="night") == {C: "unchanged"}
    assert not engine.renderer.alive(C)
    assert rec.owed is None
    lamp.ignore_commands = False
    await advance(hass, freezer, 60)
    assert len(lamp.calls) == 1 and state(hass, C).state == "off"
    assert state(hass, STATUS).state == "ok"


async def test_an_expiry_that_may_not_light_the_lamp_stops_the_render_in_flight(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 2 #1 B: both layers expire, the safety rule records what the lamp shows
    and sends nothing. A render still retrying the expired layer must stop."""
    engine = await start(hass, [A])
    lamp = lights["a"]
    await layers(hass, "set", entity_id=A, layer="evening", priority=40, brightness=30, ttl=3)
    await settle(hass)
    assert brightness(hass, A) == 30
    monkeypatch.setattr(render_module, "RETRY_BACKOFF_S", PARKED)
    lamp.ignore_commands = True
    await layers(hass, "set", entity_id=A, layer="movie", priority=50, brightness=10, ttl=3)
    await let_run()
    assert engine.renderer.alive(A)
    sent = len(lamp.calls)

    await advance(hass, freezer, 3.5)
    rec = engine.records[A]
    assert rec.layers == {}
    assert rec.base.brightness == 30 and rec.base_source == "expiry"
    assert not engine.renderer.alive(A) and rec.owed is None
    lamp.ignore_commands = False
    await advance(hass, freezer, 60)
    assert len(lamp.calls) == sent and brightness(hass, A) == 30
    assert state(hass, STATUS).state == "ok"


async def test_a_render_stops_at_its_next_retry_once_its_command_is_no_longer_wanted(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The renderer's own backstop (review 2 #1): before each retry it checks that its
    command is still the lamp's; a record changed behind its back stops it."""
    engine = await start(hass, [A])
    monkeypatch.setattr(render_module, "RETRY_BACKOFF_S", (5.0,))
    lights["a"].ignore_commands = True
    await layers(hass, "set", entity_id=A, layer="dim", priority=40, brightness=30)
    await asyncio.sleep(0.05)                        # attempt 1 ignored; waiting to retry
    assert engine.renderer.alive(A)
    engine.records[A].layers.clear()                 # not through any engine path
    lights["a"].ignore_commands = False
    await advance(hass, freezer, 6)
    assert not engine.renderer.alive(A)
    assert len(lights["a"].calls) == 1 and engine.records[A].owed is None
    assert brightness(hass, A) == 102


# --------------------------------------------------------------------------- R1-4


async def test_a_render_started_during_the_grace_is_not_judged_as_an_old_debt(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 1 #4: after a reload the TV's clear (a restore to 102) starts during the
    grace; the lamp reports late. At the grace end its owed is Layers' own render in
    flight, not a debt for decide_return, which would record the TV dim as the base."""
    from . import test_lifecycle as lc

    lights["a"].push(brightness=64)
    await hass.async_block_till_done()
    now = lc.now_ts()
    lc.seed(hass_storage, {
        A: lc.Record(A, base=lc.BASE_A, base_source="user",
                     layers={"tv": lc.make_layer("tv", 40, Command(None, 64), mode="adjust",
                                                 seq=1, set_at=now - 900)},
                     observed=lc.shows(hass, A)),
    })
    entry = await lc.setup_layers(hass, [A], apply=False, end_grace=False)
    engine = lc.engine_of(entry)
    monkeypatch.setattr(render_module, "RETRY_BACKOFF_S", PARKED)
    lights["a"].report_after = 10                      # past the grace and HA's context reuse
    await tick(hass, freezer, 1)
    assert (await lc.layers(hass, "clear", {"entity_id": A, "layer": "tv"}))["entities"] == {
        A: "queued"}
    await tick(hass, freezer, 0)                       # attempt 1 sent, not yet reported
    assert engine.renderer.alive(A) and brightness(hass, A) == 64

    await tick(hass, freezer, 4.5)                     # the grace ends, the report still to come
    assert not engine.in_grace
    rec = engine.records[A]
    assert rec.base == lc.BASE_A, "the TV dim became the base"
    assert not any(d["kind"].startswith("return:") for d in engine.decisions)
    await tick(hass, freezer, 6)                       # the late report: noise for the render
    assert brightness(hass, A) == 102 and "noise" in [d["kind"] for d in engine.decisions]
    await tick(hass, freezer, 20)                      # the parked retry verifies it
    lc.assert_shows_base_a(hass)
    assert rec.base == lc.BASE_A and rec.owed is None and not engine.renderer.alive(A)
    await tick(hass, freezer, 11)                      # the lamp's last (late) report


# --------------------------------------------------------------------------- R1-6 / R3-4


async def test_a_late_recheck_judges_a_persons_pending_change_first(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    freezer: TickingDateTimeFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 1 #6 B / review 3 #4: a person turns a Hue lamp up (no context) shortly
    before the late re-check: the re-check must not put our dim back over them."""
    engine = await start(hass, [A])
    engine._platforms[A] = "hue"                     # noqa: SLF001
    monkeypatch.setattr(render_module, "LATE_RECHECK_S", 2.0)
    await layers(hass, "set", entity_id=A, layer="dim", priority=40, brightness=30)
    await settle(hass)
    lights["a"].push(brightness=180)                 # a Hue dimmer: no context
    await hass.async_block_till_done()
    assert kinds(engine, A)[-1] == "debounce"

    await advance(hass, freezer, 2.5)                # the re-check comes before the debounce ends
    rec = engine.records[A]
    assert "late_recheck_failed" not in kinds(engine, A)
    assert rec.layers == {} and "dim" in rec.tombstones and rec.base.brightness == 180
    await advance(hass, freezer, 5)
    assert brightness(hass, A) == 180
    assert len(sent_by_layers(lights["a"], renders)) == 1


async def test_an_expiry_judges_a_persons_pending_change_first(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    freezer: TickingDateTimeFactory,
) -> None:
    """Review 1 #6 F: a person turns the lamp up a second before an adjust layer's TTL."""
    engine = await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="dim", priority=40, mode="adjust",
                 brightness=30, ttl=5)
    await settle(hass)
    await advance(hass, freezer, 4)
    lights["a"].push(brightness=200)                 # no context: a debounce
    await hass.async_block_till_done()
    await advance(hass, freezer, 1.5)                # the TTL fires inside the debounce
    rec = engine.records[A]
    assert rec.base.brightness == 200 and rec.base_source == "device"
    await advance(hass, freezer, 5)
    assert brightness(hass, A) == 200
    assert len(sent_by_layers(lights["a"], renders)) == 1


async def test_a_replay_rerender_judges_a_persons_pending_change_first(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    freezer: TickingDateTimeFactory,
) -> None:
    """Review 1 #6 (the replay path): a no-context change pending when the replay
    re-render is due is judged before the layers are put back."""
    engine = await start(hass, [A])
    press = Context(parent_id=Context().id)
    await light(hass, "turn_on", A, press, brightness=180)
    await advance(hass, freezer, 1)
    await layers(hass, "set", entity_id=A, layer="tv", priority=40, state="off")
    await settle(hass)
    await advance(hass, freezer, 2)
    await light(hass, "turn_on", A, press, brightness=180)     # the loop re-sends: replayed
    await settle(hass)
    assert engine.records[A].last_external.policy == "replay"
    await advance(hass, freezer, 18.5)
    lights["a"].push(brightness=90)                  # a person, at a dimmer
    await hass.async_block_till_done()
    await advance(hass, freezer, 2)                  # the re-render is due inside the debounce
    rec = engine.records[A]
    assert rec.layers == {} and "tv" in rec.tombstones
    await advance(hass, freezer, 5)
    assert state(hass, A).state == "on" and brightness(hass, A) == 90
    assert len(sent_by_layers(lights["a"], renders)) == 1


# --------------------------------------------------------------------------- R1-7 / R3-7


async def test_a_reversal_of_our_retry_is_a_person_not_a_second_bridge_revert(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    freezer: TickingDateTimeFactory,
) -> None:
    """Review 1 #7 / review 3 #7: one late retry per command. A person pressing Off
    twice within the Hue window after our ON is not fought a second time."""
    engine = await start(hass, [C])
    engine._platforms[C] = "hue"                     # noqa: SLF001
    lamp = lights["c"]
    await layers(hass, "set", entity_id=C, layer="hold", priority=40, brightness=150)
    await settle(hass)
    await advance(hass, freezer, 30)
    lamp.push(is_on=False)                           # 1st no-context off: retried
    await settle(hass)
    assert state(hass, C).state == "on" and kinds(engine, C).count("failed_delivery") == 1
    await advance(hass, freezer, 30)
    lamp.push(is_on=False)                           # 2nd, against the retry: a person
    await settle(hass)
    assert "late_retry_spent" in kinds(engine, C)
    await advance(hass, freezer, 3.5)
    rec = engine.records[C]
    assert state(hass, C).state == "off"
    assert rec.base == OFF_COMMAND and "hold" in rec.tombstones
    assert [s for s, _ in sent_by_layers(lamp, renders)] == ["turn_on", "turn_on"]
    await advance(hass, freezer, 60)
    assert state(hass, C).state == "off"


# --------------------------------------------------------------------------- R1-8


async def test_an_expiry_does_not_push_a_lamp_left_unsynced_by_observe_only(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
) -> None:
    """Review 1 #8: only layers.sync pushes an unsynced lamp (SPEC 2, 7.5), even when
    an expiry leaves a set layer holding it."""
    engine = await start(hass, [A], apply=False)
    assert await layers(hass, "set", entity_id=A, layer="tv", priority=40,
                        state="off") == {A: "shadow"}
    assert await layers(hass, "set", entity_id=A, layer="sig", priority=70, brightness=255,
                        ttl=30) == {A: "shadow"}
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.layers_apply"},
                                   blocking=True)
    await advance(hass, freezer, 31)
    rec = engine.records[A]
    assert set(rec.layers) == {"tv"} and rec.diverged == DIV_UNSYNCED
    assert lights["a"].calls == []
    assert await layers(hass, "sync", entity_id=A) == {A: "queued"}
    await settle(hass)
    assert state(hass, A).state == "off" and rec.diverged is None


async def test_an_expiry_does_not_snap_a_kept_manual_change_back(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event], person: Context,
    freezer: TickingDateTimeFactory,
) -> None:
    """Review 1 #8: base_keep_layers keeps a person's change on show until layers.sync;
    a signal expiring above a hold must not turn the lamp off under them."""
    engine = await start(hass, [A], base_keep=[A])
    await layers(hass, "set", entity_id=A, layer="tv", priority=40, state="off")
    await layers(hass, "set", entity_id=A, layer="sig", priority=70, brightness=255, ttl=30)
    await settle(hass)
    assert brightness(hass, A) == 255
    await light(hass, "turn_on", A, person, brightness=90)
    await settle(hass)
    rec = engine.records[A]
    assert rec.diverged == DIV_MANUAL_KEEP and set(rec.layers) == {"tv", "sig"}
    await advance(hass, freezer, 31)
    assert set(rec.layers) == {"tv"} and rec.diverged == DIV_MANUAL_KEEP
    assert state(hass, A).state == "on" and brightness(hass, A) == 90
    assert len(sent_by_layers(lights["a"], renders)) == 2


# --------------------------------------------------------------------------- R2-3


async def test_a_lamp_writing_its_state_inside_our_call_sees_a_live_render(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 2 #3: an optimistic lamp writes its state synchronously from inside our
    light call. The render must already count as alive then, so an off-target report
    is noise for its verification, not a debounce that takes our own layer back."""
    engine = await start(hass, [A])
    lamp = lights["a"]
    real = lamp.async_turn_on

    async def clamps(**kwargs: Any) -> None:
        await real(**{**kwargs, "brightness": 200})  # it can only do 200

    monkeypatch.setattr(lamp, "async_turn_on", clamps)
    await layers(hass, "set", entity_id=A, layer="dim", priority=40, brightness=110)
    assert kinds(engine, A)[0] == "noise"
    await settle(hass, 0.5)
    await advance(hass, freezer, 5)
    rec = engine.records[A]
    assert "dim" in rec.layers and rec.tombstones == {}
    assert "debounce" not in kinds(engine, A) and "external" not in kinds(engine, A)
    assert rec.diverged == "delivery"                # it never took 110: a failure, loudly


# --------------------------------------------------------------------------- R2-4


@pytest.mark.parametrize("error", [OSError, KeyError, ValueError])
async def test_any_exception_from_the_lamps_integration_is_retried(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    monkeypatch: pytest.MonkeyPatch, error: type[Exception],
) -> None:
    """Review 2 #4: not only HomeAssistantError: verification decides, and retries."""
    engine = await start(hass, [A])
    lamp = lights["a"]
    real = lamp.async_turn_off
    seen = {"n": 0}

    async def flaky(**kwargs: Any) -> None:
        seen["n"] += 1
        if seen["n"] == 1:
            raise error("the bridge dropped the request")
        await real(**kwargs)

    monkeypatch.setattr(lamp, "async_turn_off", flaky)
    assert await layers(hass, "set", entity_id=A, layer="hold", priority=40,
                        state="off") == {A: "queued"}
    await settle(hass, 0.3)
    assert state(hass, A).state == "off"
    assert [e.data["attempt"] for e in renders] == [1, 2]
    assert engine.records[A].owed is None and state(hass, STATUS).state == "ok"


async def test_a_lamp_that_always_raises_ends_failed_not_pending_forever(
    hass: HomeAssistant, lights: dict[str, FakeLamp], monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await start(hass, [A])
    failures = async_capture_events(hass, EVENT_RENDER_FAILED)

    async def broken(**kwargs: Any) -> None:
        raise OSError("gone")

    monkeypatch.setattr(lights["a"], "async_turn_off", broken)
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, state="off")
    await settle(hass, 0.5)
    assert len(failures) == 1 and engine.records[A].owed is None
    assert state(hass, STATUS).state == "failed"


# --------------------------------------------------------------------------- R1-9 / R2-9


async def test_one_lamps_error_marks_only_that_lamp_untrusted_and_sync_clears_it(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 1 #9 / review 2 #9: SPEC 7.1: handlers never raise; a failing lamp is
    marked untrusted (shown, and cleared by layers.sync), the others go on."""
    engine = await start(hass, [A, B])
    real = policy_module.expire

    def broken_for_a(rec: Any, *args: Any, **kwargs: Any) -> Any:
        if rec.entity_id == A:
            raise RuntimeError("synthetic")
        return real(rec, *args, **kwargs)

    await layers(hass, "set", entity_id=A, layer="a1", priority=40, brightness=30, ttl=10)
    await layers(hass, "set", entity_id=B, layer="b1", priority=40, brightness=30, ttl=20)
    await settle(hass)
    monkeypatch.setattr(policy_module, "expire", broken_for_a)
    await advance(hass, freezer, 11)
    assert engine.records[A].untrusted and "a1" in engine.records[A].layers
    assert engine.describe([A])[A]["untrusted"] is True
    assert state(hass, STATUS).attributes["untrusted"] == [A]
    await advance(hass, freezer, 10)
    assert engine.records[B].layers == {}                                 # B's TTL still ran
    assert engine.records[B].base_source == "expiry"      # (and never brightens: B stays at 30)

    monkeypatch.setattr(policy_module, "expire", real)
    await layers(hass, "sync", entity_id=A)
    await advance(hass, freezer, 1)
    assert not engine.records[A].untrusted and engine.records[A].layers == {}
    assert state(hass, STATUS).attributes["untrusted"] == []


async def test_marking_a_lamp_untrusted_stops_its_render(
    hass: HomeAssistant, lights: dict[str, FakeLamp], monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await start(hass, [A])
    monkeypatch.setattr(render_module, "RETRY_BACKOFF_S", PARKED)
    lights["a"].ignore_commands = True
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, state="off")
    await let_run()
    assert engine.renderer.alive(A)
    engine._mark_untrusted(A)                        # noqa: SLF001
    assert not engine.renderer.alive(A)
    assert engine._render(A, reason="expiry", parent_id=None, deferred=True) == "unchanged"  # noqa: SLF001


# --------------------------------------------------------------------------- R2-11


async def test_unloading_removes_the_render_failed_repair_issues(
    hass: HomeAssistant, lights: dict[str, FakeLamp],
) -> None:
    """Review 2 #11: a repair issue belongs to the engine that raised it."""
    engine = await start(hass, [A])
    lights["a"].ignore_commands = True
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, state="off")
    await settle(hass, 0.5)
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"render_failed_{A}") is not None
    assert await hass.config_entries.async_unload(engine.entry.entry_id)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, f"render_failed_{A}") is None
