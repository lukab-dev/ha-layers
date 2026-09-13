"""Lifecycle: leases and expiry, startup from a stored model, the apply switch,
reload, removal, shutdown, and what the Store keeps (SPEC 2, 5.4, 7.3-7.5).

Time: the whole module runs under a *ticking* freezegun clock. Renders sleep for
a few real milliseconds (``fast_render``), so they finish on their own, while
``advance`` jumps ``utcnow`` and the loop clock together past a TTL, the startup
grace or a return's settle window. Tests end the startup grace through its real
timer, never by calling ``_end_grace`` directly.

Startup tests pre-seed ``hass_storage`` with the Store document Layers would have
written before Home Assistant stopped, then set the integration up while Home
Assistant is already running (grace = ``RETURN_SETTLE_S``).
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from typing import Any

import pytest

from homeassistant.components.light import ColorMode
from homeassistant.const import EVENT_CALL_SERVICE, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Context, CoreState, Event, HomeAssistant, State, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from homeassistant.util.async_ import get_scheduled_timer_handles
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockUser,
    async_fire_time_changed,
    mock_restore_cache,
)

from custom_components.layers import render as render_module
from custom_components.layers.const import DOMAIN, EVENT_RENDER, STORAGE_KEY
from custom_components.layers.logic.capability import observed_from_state
from custom_components.layers.logic.model import (
    DIV_UNSYNCED,
    MODE_ADJUST,
    MODE_SET,
    ON,
    ON_EXPIRE_SAFE,
    OWED_ON_MAX_AGE_S,
    RETURN_SETTLE_S,
    STARTUP_GRACE_S,
    Color,
    Command,
    Layer,
    Observed,
    OFF_COMMAND,
    Owed,
    Record,
    Tombstone,
)
from custom_components.layers.store import LayersStore

from .conftest import FakeLamp, settle, setup_layers

pytestmark = pytest.mark.freeze_time(tick=True)

A, B, C, D = "light.lamp_a", "light.lamp_b", "light.lamp_c", "light.lamp_d"
APPLY = "switch.layers_apply"
STATUS = "sensor.layers_status"
BASE_A = Command(ON, 102, Color.kelvin(2700))   # what lamp_a shows when the test starts
SIGNAL_XY = (0.6, 0.35)
SIGNAL = Command(ON, 255, Color.xy(*SIGNAL_XY))


# --------------------------------------------------------------------------- helpers


def now_ts() -> float:
    return dt_util.utcnow().timestamp()


async def advance(hass: HomeAssistant, freezer: Any, seconds: float) -> None:
    """Jump the clock (utcnow and the loop clock) forward and let what fell due run."""
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await settle(hass)


def engine_of(entry: MockConfigEntry) -> Any:
    return entry.runtime_data.engine


async def start(hass: HomeAssistant, freezer: Any, entities: list[str], **kwargs: Any) -> MockConfigEntry:
    """Set Layers up and end its startup grace through the real timer."""
    entry = await setup_layers(hass, entities, end_grace=False, **kwargs)
    assert engine_of(entry).in_grace
    await advance(hass, freezer, RETURN_SETTLE_S + 0.5)
    assert not engine_of(entry).in_grace
    return entry


async def layers(hass: HomeAssistant, service: str, data: dict[str, Any],
                 context: Context | None = None) -> dict[str, Any]:
    return await hass.services.async_call(DOMAIN, service, data, blocking=True,
                                          return_response=True, context=context)


def watch_light_calls(hass: HomeAssistant) -> list[tuple[str, dict[str, Any]]]:
    """Every light.* service call from now on, by anyone: (service, data)."""
    seen: list[tuple[str, dict[str, Any]]] = []

    @callback
    def _record(event: Event) -> None:
        if event.data.get("domain") == "light":
            seen.append((event.data.get("service"), dict(event.data.get("service_data") or {})))

    hass.bus.async_listen(EVENT_CALL_SERVICE, _record)
    return seen


def services(calls: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [service for service, _ in calls]


def shows(hass: HomeAssistant, eid: str) -> Observed:
    """What the lamp reports right now, as Layers would have stored it."""
    state = hass.states.get(eid)
    return observed_from_state(state.state, state.attributes, now_ts())


def brightness(hass: HomeAssistant, eid: str) -> int | None:
    return hass.states.get(eid).attributes.get("brightness")


def assert_shows_base_a(hass: HomeAssistant) -> None:
    state = hass.states.get(A)
    assert state.state == "on"
    assert state.attributes["brightness"] == 102
    assert state.attributes["color_mode"] == ColorMode.COLOR_TEMP
    assert state.attributes["color_temp_kelvin"] == 2700


def make_layer(layer_id: str, priority: int, command: Command, *, seq: int, set_at: float,
               expires_at: float | None = None, mode: str = MODE_SET,
               on_expire: str = ON_EXPIRE_SAFE, owner: str | None = None) -> Layer:
    return Layer(id=layer_id, priority=priority, mode=mode, requested=command, command=command,
                 seq=seq, set_at=set_at, expires_at=expires_at, owner=owner, on_expire=on_expire)


def seed(hass_storage: dict[str, Any], records: dict[str, Record], *, apply: bool = True,
         seq: int = 50, minor_version: int = 1) -> dict[str, Any]:
    """The Store document Layers wrote before Home Assistant stopped."""
    data = {
        "seq": seq,
        "apply": apply,
        "entities": {eid: rec.to_json() for eid, rec in records.items()},
    }
    hass_storage[STORAGE_KEY] = {
        "version": 1, "minor_version": minor_version, "key": STORAGE_KEY, "data": deepcopy(data),
    }
    return data


def stored(hass_storage: dict[str, Any]) -> dict[str, Any]:
    return hass_storage[STORAGE_KEY]["data"]


def _belongs_to_layers(obj: Any, depth: int = 0) -> bool:
    if obj is None or depth > 4:
        return False
    for module in (getattr(obj, "__module__", None), type(obj).__module__):
        if isinstance(module, str) and module.startswith("custom_components.layers"):
            return True
    for attr in ("__self__", "__func__", "job", "target"):
        inner = getattr(obj, attr, None)
        if inner is not None and inner is not obj and _belongs_to_layers(inner, depth + 1):
            return True
    return False


def layers_timers(hass: HomeAssistant) -> list[Any]:
    """Loop timers still scheduled that would call back into Layers."""
    return [
        handle for handle in get_scheduled_timer_handles(hass.loop)
        if not handle.cancelled()
        and (_belongs_to_layers(handle._callback)
             or any(_belongs_to_layers(arg) for arg in (handle._args or ())))
    ]


# =========================================================================== expiry
# SPEC 5.4: an expiry never turns a lamp on or brightens it, unless on_expire: render
# or a set layer still holds the lamp. Plan: "TV expiry is suppressed, the trash
# colour goes back to base, and a nightlight goes off".


async def test_expiry_of_an_off_hold_does_not_turn_the_lamp_back_on(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any
) -> None:
    entry = await start(hass, freezer, [A])
    engine = engine_of(entry)
    calls = watch_light_calls(hass)
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off", "ttl": 60})
    await settle(hass)
    assert hass.states.get(A).state == "off"
    assert services(calls) == ["turn_off"]

    await advance(hass, freezer, 58)
    assert "hold" in engine.records[A].layers
    await advance(hass, freezer, 3)

    rec = engine.records[A]
    assert rec.layers == {}
    assert rec.base == OFF_COMMAND and rec.base_source == "expiry"
    assert hass.states.get(A).state == "off"
    await advance(hass, freezer, 600)
    assert hass.states.get(A).state == "off"
    assert services(calls) == ["turn_off"], "the expiry sent something"


async def test_expiry_of_a_bright_signal_returns_the_lamp_to_its_base(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any
) -> None:
    entry = await start(hass, freezer, [A])
    engine = engine_of(entry)
    calls = watch_light_calls(hass)
    await layers(hass, "set", {"entity_id": A, "layer": "signal", "priority": 70, "brightness": 255,
                               "xy_color": list(SIGNAL_XY), "ttl": 60})
    await settle(hass)
    assert brightness(hass, A) == 255
    assert hass.states.get(A).attributes["color_mode"] == ColorMode.XY

    await advance(hass, freezer, 61)

    assert_shows_base_a(hass)
    rec = engine.records[A]
    assert rec.layers == {}
    assert rec.base == BASE_A and rec.base_source == "startup"
    assert rec.owed is None and rec.diverged is None
    assert services(calls) == ["turn_on", "turn_on"]
    assert calls[-1][1] == {"entity_id": A, "brightness": 102, "color_temp_kelvin": 2700}
    assert hass.states.get(STATUS).state == "ok"


async def test_expiry_of_a_nightlight_over_an_off_base_turns_the_lamp_off(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any
) -> None:
    entry = await start(hass, freezer, [C])
    engine = engine_of(entry)
    calls = watch_light_calls(hass)
    await layers(hass, "set", {"entity_id": C, "layer": "nightlight", "priority": 50, "state": "on",
                               "brightness": 13, "ttl": 600})
    await settle(hass)
    assert hass.states.get(C).state == "on" and brightness(hass, C) == 13

    await advance(hass, freezer, 601)

    assert hass.states.get(C).state == "off"
    assert engine.records[C].layers == {}
    assert engine.records[C].base == OFF_COMMAND
    assert services(calls) == ["turn_on", "turn_off"]


async def test_on_expire_render_restores_what_is_below_even_when_that_lights_the_lamp(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any
) -> None:
    entry = await start(hass, freezer, [A])
    engine = engine_of(entry)
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off",
                               "ttl": 60, "on_expire": "render"})
    await settle(hass)
    assert hass.states.get(A).state == "off"

    await advance(hass, freezer, 61)

    assert_shows_base_a(hass)
    assert engine.records[A].layers == {}
    assert engine.records[A].base == BASE_A


async def test_expiry_under_a_holding_layer_renders_the_holder(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any
) -> None:
    entry = await start(hass, freezer, [A])
    engine = engine_of(entry)
    calls = watch_light_calls(hass)
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off"})
    await layers(hass, "set", {"entity_id": A, "layer": "signal", "priority": 70, "brightness": 255,
                               "xy_color": list(SIGNAL_XY), "ttl": 60})
    await settle(hass)
    assert brightness(hass, A) == 255

    await advance(hass, freezer, 61)

    assert hass.states.get(A).state == "off"
    assert set(engine.records[A].layers) == {"hold"}
    assert services(calls)[-1] == "turn_off"


async def test_expiry_never_lights_a_lamp_through_an_adjust_layer(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any
) -> None:
    entry = await start(hass, freezer, [A])
    engine = engine_of(entry)
    await layers(hass, "set", {"entity_id": A, "layer": "dim", "priority": 20, "mode": MODE_ADJUST,
                               "brightness": 30})
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 60, "state": "off", "ttl": 60})
    await settle(hass)
    assert hass.states.get(A).state == "off"
    calls = watch_light_calls(hass)

    await advance(hass, freezer, 61)

    rec = engine.records[A]
    assert hass.states.get(A).state == "off"
    assert calls == []
    assert set(rec.layers) == {"dim"}
    assert rec.base == OFF_COMMAND


async def test_each_lamp_expires_at_its_own_time(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any
) -> None:
    entry = await start(hass, freezer, [A, C])
    engine = engine_of(entry)
    await layers(hass, "set", {"entity_id": A, "layer": "signal", "priority": 70, "brightness": 255,
                               "xy_color": list(SIGNAL_XY), "ttl": 30})
    await layers(hass, "set", {"entity_id": C, "layer": "nightlight", "priority": 50, "brightness": 13,
                               "ttl": 90})
    await settle(hass)

    await advance(hass, freezer, 31)
    assert_shows_base_a(hass)
    assert hass.states.get(C).state == "on" and brightness(hass, C) == 13
    assert "nightlight" in engine.records[C].layers

    await advance(hass, freezer, 60)
    assert hass.states.get(C).state == "off"
    assert engine.records[C].layers == {}


async def test_a_signal_that_expires_while_its_lamp_is_away_is_delivered_when_it_returns(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any
) -> None:
    entry = await start(hass, freezer, [A])
    engine = engine_of(entry)
    await layers(hass, "set", {"entity_id": A, "layer": "signal", "priority": 70, "brightness": 255,
                               "xy_color": list(SIGNAL_XY), "ttl": 60})
    await settle(hass)
    lights["a"].set_available(False)
    await hass.async_block_till_done()
    calls = watch_light_calls(hass)

    await advance(hass, freezer, 61)

    rec = engine.records[A]
    assert rec.layers == {}
    assert rec.owed is not None and rec.owed.target == BASE_A
    assert rec.base == BASE_A
    assert calls == []

    lights["a"].set_available(True)   # back, untouched: still showing the signal
    await hass.async_block_till_done()
    await advance(hass, freezer, RETURN_SETTLE_S + 0.5)

    assert_shows_base_a(hass)
    assert services(calls) == ["turn_on"]
    assert rec.owed is None


async def test_an_off_hold_that_expires_while_its_lamp_is_away_does_not_light_it_on_return(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any
) -> None:
    entry = await start(hass, freezer, [A])
    engine = engine_of(entry)
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off", "ttl": 60})
    await settle(hass)
    lights["a"].set_available(False)
    await hass.async_block_till_done()
    calls = watch_light_calls(hass)

    await advance(hass, freezer, 61)
    rec = engine.records[A]
    assert rec.layers == {} and rec.owed is None
    assert rec.base == OFF_COMMAND

    lights["a"].set_available(True)
    await hass.async_block_till_done()
    await advance(hass, freezer, RETURN_SETTLE_S + 0.5)
    await advance(hass, freezer, 600)
    assert hass.states.get(A).state == "off"
    assert calls == []


async def test_an_expiry_while_apply_is_off_changes_the_model_and_sends_nothing(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any
) -> None:
    entry = await start(hass, freezer, [A])
    engine = engine_of(entry)
    await layers(hass, "set", {"entity_id": A, "layer": "signal", "priority": 70, "brightness": 255,
                               "xy_color": list(SIGNAL_XY), "ttl": 60})
    await settle(hass)
    await hass.services.async_call("switch", "turn_off", {"entity_id": APPLY}, blocking=True)
    calls = watch_light_calls(hass)

    await advance(hass, freezer, 61)

    rec = engine.records[A]
    assert rec.layers == {}
    assert rec.base == BASE_A           # the signal did not become the base
    assert rec.diverged == DIV_UNSYNCED
    assert brightness(hass, A) == 255   # nothing was sent
    assert calls == []
    assert hass.states.get(STATUS).state == "shadow"

    await hass.services.async_call("switch", "turn_on", {"entity_id": APPLY}, blocking=True)
    await advance(hass, freezer, 30)
    assert calls == [], "turning apply on sent something by itself"

    await layers(hass, "sync", {"entity_id": A})
    await settle(hass)
    assert_shows_base_a(hass)


async def test_a_tombstone_runs_out_with_the_layer_it_replaced(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_admin_user: MockUser
) -> None:
    entry = await start(hass, freezer, [A])
    engine = engine_of(entry)
    signal = {"entity_id": A, "layer": "signal", "priority": 70, "brightness": 255,
              "xy_color": list(SIGNAL_XY), "ttl": 60}
    await layers(hass, "set", signal)
    await settle(hass)
    await hass.services.async_call("light", "turn_on", {"entity_id": A, "brightness": 30},
                                   blocking=True, context=Context(user_id=hass_admin_user.id))
    await settle(hass)
    rec = engine.records[A]
    assert rec.layers == {} and "signal" in rec.tombstones
    calls = watch_light_calls(hass)

    result = await layers(hass, "set", signal)
    assert result["entities"][A] == "skipped_tombstoned"

    await advance(hass, freezer, 61)
    assert "signal" not in rec.tombstones
    assert brightness(hass, A) == 30
    assert calls == []

    result = await layers(hass, "set", signal)
    await settle(hass)
    assert result["entities"][A] == "queued"
    assert brightness(hass, A) == 255


# =========================================================================== until


async def test_until_expires_the_layer_at_that_time(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any
) -> None:
    entry = await start(hass, freezer, [A])
    engine = engine_of(entry)
    until = dt_util.utcnow() + timedelta(seconds=90)
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off",
                               "until": until.isoformat()})
    await settle(hass)
    assert engine.records[A].layers["hold"].expires_at == pytest.approx(until.timestamp(), abs=0.001)

    await advance(hass, freezer, 60)
    assert "hold" in engine.records[A].layers
    await advance(hass, freezer, 31)
    assert engine.records[A].layers == {}
    assert hass.states.get(A).state == "off"       # the safety rule applies to until as to ttl
    assert engine.records[A].base == OFF_COMMAND


async def test_a_naive_until_is_local_time(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any
) -> None:
    entry = await start(hass, freezer, [A])
    engine = engine_of(entry)
    zone = dt_util.get_default_time_zone()
    local = (dt_util.now() + timedelta(minutes=2)).replace(microsecond=0, tzinfo=None)
    assert zone.utcoffset(local) != timedelta(0), "the test needs a time zone that is not UTC"
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off",
                               "until": local.isoformat()})
    expected = local.replace(tzinfo=zone).timestamp()
    assert engine.records[A].layers["hold"].expires_at == pytest.approx(expected, abs=0.001)


async def test_an_until_in_the_past_is_refused_and_changes_nothing(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any
) -> None:
    entry = await start(hass, freezer, [A])
    engine = engine_of(entry)
    calls = watch_light_calls(hass)
    past = dt_util.utcnow() - timedelta(seconds=5)
    with pytest.raises(ServiceValidationError):
        await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off",
                                   "until": past.isoformat()})
    await settle(hass)
    assert engine.records[A].layers == {}
    assert calls == []


# =========================================================================== leases


@pytest.mark.parametrize("renewal", [{}, {"only_if_present": True}], ids=["same_set", "only_if_present"])
async def test_renewing_a_lease_keeps_the_layer_and_sends_nothing(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, renewal: dict[str, Any]
) -> None:
    """The nightlight pattern: a 10 min lease renewed every 5 min while the condition holds."""
    entry = await start(hass, freezer, [C])
    engine = engine_of(entry)
    renders: list[Event] = []
    hass.bus.async_listen(EVENT_RENDER, renders.append)
    nightlight = {"entity_id": C, "layer": "nightlight", "priority": 50, "state": "on",
                  "brightness": 13, "ttl": 600, "owner": "nightlight"}
    await layers(hass, "set", nightlight)
    await settle(hass)
    assert brightness(hass, C) == 13
    layer = engine.records[C].layers["nightlight"]
    seq, set_at, first_expiry = layer.seq, layer.set_at, layer.expires_at
    assert len(renders) == 1
    calls = watch_light_calls(hass)

    await advance(hass, freezer, 300)
    result = await layers(hass, "set", {**nightlight, **renewal})
    await settle(hass)
    assert result["entities"][C] == "unchanged"
    layer = engine.records[C].layers["nightlight"]
    assert layer.expires_at == pytest.approx(first_expiry + 300, abs=1)
    assert (layer.seq, layer.set_at) == (seq, set_at)

    await advance(hass, freezer, 350)                 # past the first lease
    assert "nightlight" in engine.records[C].layers
    assert hass.states.get(C).state == "on" and brightness(hass, C) == 13
    assert calls == [] and len(renders) == 1

    await advance(hass, freezer, 260)                 # the renewed lease runs out
    assert engine.records[C].layers == {}
    assert hass.states.get(C).state == "off"
    assert services(calls) == ["turn_off"]


async def test_a_renewal_after_the_lease_ran_out_does_not_recreate_the_layer(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any
) -> None:
    entry = await start(hass, freezer, [C])
    engine = engine_of(entry)
    nightlight = {"entity_id": C, "layer": "nightlight", "priority": 50, "brightness": 13, "ttl": 600}
    await layers(hass, "set", nightlight)
    await settle(hass)
    await advance(hass, freezer, 601)
    assert hass.states.get(C).state == "off"
    calls = watch_light_calls(hass)

    result = await layers(hass, "set", {**nightlight, "only_if_present": True})
    await settle(hass)
    assert result["entities"][C] == "skipped_absent"
    assert engine.records[C].layers == {}
    assert calls == []


# =========================================================================== startup
# SPEC 7.4: the Store is loaded and nothing is sent; after the grace, in order:
# expired layers (expiry rule), renders in flight (left alone), owed lamps
# (decide_return), lamps without layers (base := observed), layered lamps that do
# not match (a change if they changed since Layers stopped, else unsynced).


async def test_nothing_is_sent_during_the_grace_and_no_context_changes_are_only_observed(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any]
) -> None:
    now = now_ts()
    lights["c"].push(is_on=True, brightness=13)
    lights["d"].push(is_on=True, brightness=255, color_temp_kelvin=6500)
    await hass.async_block_till_done()
    seed(hass_storage, {
        # a hold that does not match what the lamp shows
        A: Record(A, base=BASE_A, base_source="user",
                  layers={"hold": make_layer("hold", 40, OFF_COMMAND, seq=1, set_at=now - 900)},
                  observed=shows(hass, A)),
        # an owed off, untouched since Home Assistant stopped
        C: Record(C, base=OFF_COMMAND, observed=shows(hass, C),
                  owed=Owed(now - 30, OFF_COMMAND, turns_on=False)),
        # a signal that ran out while Home Assistant was down
        D: Record(D, base=OFF_COMMAND,
                  layers={"signal": make_layer("signal", 70, Command(ON, 255, Color.kelvin(6500)),
                                               seq=2, set_at=now - 7200, expires_at=now - 60)},
                  observed=shows(hass, D)),
    })
    calls = watch_light_calls(hass)
    entry = await setup_layers(hass, [A, B, C, D], apply=False, end_grace=False)
    engine = engine_of(entry)
    assert engine.apply is True and engine.in_grace

    await advance(hass, freezer, 1)
    lights["a"].push(brightness=60)                        # integrations re-reporting
    lights["a"].push(is_on=False)
    lights["a"].push(is_on=True, brightness=80)
    lights["b"].push(brightness=10)
    await advance(hass, freezer, 2.5)                      # longer than DEBOUNCE_S, inside the grace

    assert engine.in_grace
    assert calls == []
    assert lights["a"].calls == [] and lights["c"].calls == [] and lights["d"].calls == []
    rec = engine.records[A]
    assert set(rec.layers) == {"hold"} and rec.tombstones == {}
    assert rec.base == BASE_A and rec.base_source == "user"
    assert rec.observed.brightness == 80
    assert not engine._pending("debounce", A)              # noqa: SLF001
    assert "signal" in engine.records[D].layers            # its TTL is held for the grace end
    assert engine.records[C].owed is not None

    await advance(hass, freezer, 2)                        # the grace ends
    assert not engine.in_grace
    # Judged once, as it settled: it shows something else than when Layers stopped, so
    # somebody changed it meanwhile (SPEC 7.4 step 5). A change like any other, nothing sent.
    assert rec.layers == {} and set(rec.tombstones) == {"hold"}
    assert rec.base == Command(ON, 80, Color.kelvin(2700)) and rec.base_source == "device"
    assert rec.diverged is None
    assert lights["a"].calls == []


async def test_a_persons_change_during_the_grace_is_still_theirs(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any],
    hass_admin_user: MockUser,
) -> None:
    now = now_ts()
    seed(hass_storage, {
        A: Record(A, base=BASE_A,
                  layers={"hold": make_layer("hold", 40, OFF_COMMAND, seq=1, set_at=now - 900)},
                  observed=shows(hass, A)),
    })
    entry = await setup_layers(hass, [A], apply=False, end_grace=False)
    engine = engine_of(entry)
    await advance(hass, freezer, 1)
    calls = watch_light_calls(hass)
    await hass.services.async_call("light", "turn_on", {"entity_id": A, "brightness": 180},
                                   blocking=True, context=Context(user_id=hass_admin_user.id))
    await advance(hass, freezer, 5)

    rec = engine.records[A]
    assert rec.layers == {} and "hold" in rec.tombstones
    assert rec.base.brightness == 180
    assert services(calls) == ["turn_on"]                  # only the person's own call
    assert brightness(hass, A) == 180


async def test_a_layer_that_expired_while_down_is_released_at_the_grace_end_not_recorded_as_base(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any]
) -> None:
    now = now_ts()
    lights["a"].push(brightness=255, color_mode=ColorMode.XY, xy_color=SIGNAL_XY)   # still showing it
    await hass.async_block_till_done()
    seed(hass_storage, {
        A: Record(A, base=BASE_A, base_source="user", base_at=now - 7200,
                  layers={"signal": make_layer("signal", 70, SIGNAL, seq=3, set_at=now - 7200,
                                               expires_at=now - 600)},
                  observed=shows(hass, A)),
    })
    calls = watch_light_calls(hass)
    entry = await setup_layers(hass, [A], apply=False, end_grace=False)
    engine = engine_of(entry)

    await advance(hass, freezer, 3)
    assert calls == []
    assert "signal" in engine.records[A].layers

    await advance(hass, freezer, 2.5)
    assert not engine.in_grace
    rec = engine.records[A]
    assert rec.layers == {}
    assert rec.base == BASE_A and rec.base_source == "user"   # the signal did not become the base
    assert_shows_base_a(hass)
    assert services(calls) == ["turn_on"]
    kinds = [d["kind"] for d in engine.decisions if d["entity_id"] == A]
    assert "expiry" in kinds
    assert not any(kind.startswith("return:") for kind in kinds), kinds


async def test_a_layer_that_expired_while_down_on_a_lamp_that_is_away_is_owed_until_it_returns(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any]
) -> None:
    now = now_ts()
    lights["a"].push(brightness=255, color_mode=ColorMode.XY, xy_color=SIGNAL_XY)
    await hass.async_block_till_done()
    at_stop = shows(hass, A)
    lights["a"].set_available(False)
    await hass.async_block_till_done()
    seed(hass_storage, {
        A: Record(A, base=BASE_A, base_source="user",
                  layers={"signal": make_layer("signal", 70, SIGNAL, seq=3, set_at=now - 7200,
                                               expires_at=now - 600)},
                  observed=at_stop),
    })
    calls = watch_light_calls(hass)
    entry = await setup_layers(hass, [A], apply=False, end_grace=False)
    engine = engine_of(entry)
    await advance(hass, freezer, RETURN_SETTLE_S + 0.5)

    rec = engine.records[A]
    assert rec.layers == {}
    assert rec.owed is not None and rec.owed.target == BASE_A
    assert rec.base == BASE_A
    assert calls == []

    lights["a"].set_available(True)       # back, untouched: still showing the signal
    await hass.async_block_till_done()
    await advance(hass, freezer, RETURN_SETTLE_S + 0.5)
    assert_shows_base_a(hass)
    assert services(calls) == ["turn_on"]


async def test_an_off_hold_that_expired_while_down_is_dropped_without_lighting_the_lamp(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any]
) -> None:
    now = now_ts()
    lights["a"].push(is_on=False)
    await hass.async_block_till_done()
    seed(hass_storage, {
        A: Record(A, base=BASE_A,
                  layers={"hold": make_layer("hold", 40, OFF_COMMAND, seq=1, set_at=now - 7200,
                                             expires_at=now - 60)},
                  observed=shows(hass, A)),
    })
    calls = watch_light_calls(hass)
    entry = await setup_layers(hass, [A], apply=False, end_grace=False)
    engine = engine_of(entry)
    await advance(hass, freezer, RETURN_SETTLE_S + 0.5)

    rec = engine.records[A]
    assert rec.layers == {}
    assert rec.base == OFF_COMMAND
    assert hass.states.get(A).state == "off"
    await advance(hass, freezer, 600)
    assert calls == []


async def test_an_owner_clearing_during_the_grace_a_layer_that_expired_while_down_restores(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any]
) -> None:
    """SPEC 5.2 / 7.4 step 1: the owner's clear is a restore; the safety rule must not swallow it."""
    now = now_ts()
    lights["a"].push(is_on=False)
    await hass.async_block_till_done()
    seed(hass_storage, {
        A: Record(A, base=BASE_A,
                  layers={"hold": make_layer("hold", 40, OFF_COMMAND, seq=1, set_at=now - 7200,
                                             expires_at=now - 60)},
                  observed=shows(hass, A)),
    })
    calls = watch_light_calls(hass)
    entry = await setup_layers(hass, [A], apply=False, end_grace=False)
    engine = engine_of(entry)
    await advance(hass, freezer, 1)
    await layers(hass, "clear", {"entity_id": A, "layer": "hold"})
    await advance(hass, freezer, 1)
    assert calls == []                                  # the render waits for the grace end

    await advance(hass, freezer, RETURN_SETTLE_S)
    assert not engine.in_grace
    assert engine.records[A].layers == {}
    assert_shows_base_a(hass)
    assert services(calls) == ["turn_on"]


@pytest.mark.parametrize("touched", [False, True], ids=["untouched", "touched_while_down"])
async def test_an_owed_off_is_delivered_at_the_grace_end_unless_the_lamp_was_touched(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any],
    touched: bool,
) -> None:
    now = now_ts()
    lights["c"].push(is_on=True, brightness=13)
    await hass.async_block_till_done()
    # A nightlight was cleared, the render (off) started, Home Assistant stopped before it was verified.
    seed(hass_storage, {
        C: Record(C, base=OFF_COMMAND, observed=shows(hass, C),
                  owed=Owed(now - 1200, OFF_COMMAND, turns_on=False)),
    })
    if touched:
        lights["c"].push(brightness=200)    # someone changed it while Home Assistant was down
        await hass.async_block_till_done()
    calls = watch_light_calls(hass)
    entry = await setup_layers(hass, [C], apply=False, end_grace=False)
    engine = engine_of(entry)
    await advance(hass, freezer, 3)
    assert calls == []

    await advance(hass, freezer, 2.5)
    rec = engine.records[C]
    assert rec.owed is None
    if touched:
        assert calls == []
        assert hass.states.get(C).state == "on" and brightness(hass, C) == 200
        assert rec.base == Command(ON, 200, Color.kelvin(2700))
    else:
        assert services(calls) == ["turn_off"]
        assert hass.states.get(C).state == "off"


async def test_an_owed_off_on_a_lamp_that_returns_during_the_grace_is_delivered_at_its_end(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any]
) -> None:
    now = now_ts()
    lights["c"].push(is_on=True, brightness=13)
    await hass.async_block_till_done()
    at_stop = shows(hass, C)
    lights["c"].set_available(False)
    await hass.async_block_till_done()
    seed(hass_storage, {
        C: Record(C, base=OFF_COMMAND, observed=at_stop, owed=Owed(now - 30, OFF_COMMAND)),
    })
    calls = watch_light_calls(hass)
    entry = await setup_layers(hass, [C], apply=False, end_grace=False)
    engine = engine_of(entry)
    await advance(hass, freezer, 2)
    lights["c"].set_available(True)         # back, untouched
    await hass.async_block_till_done()
    await advance(hass, freezer, 1)
    assert calls == []                      # not judged inside the grace

    await advance(hass, freezer, 2.5)
    assert not engine.in_grace
    assert services(calls) == ["turn_off"]
    assert hass.states.get(C).state == "off"


@pytest.mark.parametrize(
    ("age", "delivered"),
    [(OWED_ON_MAX_AGE_S + 60, False), (60, True)],
    ids=["older_than_10_min", "fresh"],
)
async def test_an_owed_on_is_delivered_at_the_grace_end_only_while_it_is_fresh(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any],
    age: float, delivered: bool,
) -> None:
    now = now_ts()
    # A hold was cleared, the render (on at 150) started, Home Assistant stopped before it landed.
    seed(hass_storage, {
        C: Record(C, base=Command(ON, 150), observed=shows(hass, C),
                  owed=Owed(now - age, Command(ON, 150), turns_on=True)),
    })
    calls = watch_light_calls(hass)
    entry = await setup_layers(hass, [C], apply=False, end_grace=False)
    engine = engine_of(entry)
    await advance(hass, freezer, RETURN_SETTLE_S + 0.5)

    rec = engine.records[C]
    assert rec.owed is None
    if delivered:
        assert services(calls) == ["turn_on"]
        assert hass.states.get(C).state == "on" and brightness(hass, C) == 150
    else:
        assert rec.base == OFF_COMMAND
        await advance(hass, freezer, 600)
        assert calls == []
        assert hass.states.get(C).state == "off"


async def test_at_the_grace_end_lamps_without_layers_take_what_they_show_as_base(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any]
) -> None:
    now = now_ts()
    seed(hass_storage, {
        # only a tombstone: persisted, with a base that has gone stale since
        B: Record(B, base=Command(ON, 50), base_source="user", observed=Observed("on", 50, "brightness"),
                  tombstones={"signal": Tombstone("signal", now - 100, expires_at=now + 3600)}),
    })
    calls = watch_light_calls(hass)
    entry = await setup_layers(hass, [A, B, C, D], apply=False, end_grace=False)
    engine = engine_of(entry)
    assert engine.records[A].base is None            # not stored: unknown until the grace ends

    await advance(hass, freezer, RETURN_SETTLE_S + 0.5)

    records = engine.records
    assert records[A].base == BASE_A and records[A].base_source == "startup"
    assert records[B].base == Command(ON, 200) and records[B].base_source == "startup"
    assert records[C].base == OFF_COMMAND
    assert records[D].base == OFF_COMMAND
    assert "signal" in records[B].tombstones
    assert all(rec.diverged is None for rec in records.values())
    assert calls == []


async def test_at_the_grace_end_a_layered_lamp_that_does_not_match_is_unsynced_not_commanded(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any]
) -> None:
    now = now_ts()
    lights["c"].push(is_on=True, brightness=13)
    await hass.async_block_till_done()
    seed(hass_storage, {
        A: Record(A, base=BASE_A,
                  layers={"hold": make_layer("hold", 40, OFF_COMMAND, seq=1, set_at=now - 900)},
                  observed=shows(hass, A)),
        C: Record(C, base=OFF_COMMAND,
                  layers={"nightlight": make_layer("nightlight", 50, Command(ON, 13), seq=2,
                                                   set_at=now - 900)},
                  observed=shows(hass, C)),
    })
    calls = watch_light_calls(hass)
    entry = await setup_layers(hass, [A, C], apply=False, end_grace=False)
    engine = engine_of(entry)
    await advance(hass, freezer, RETURN_SETTLE_S + 0.5)
    await advance(hass, freezer, 600)

    assert engine.records[A].diverged == DIV_UNSYNCED
    assert set(engine.records[A].layers) == {"hold"}
    assert engine.records[C].diverged is None          # it shows its layer
    assert hass.states.get(A).state == "on"
    assert calls == []

    result = await layers(hass, "sync", {"entity_id": A})
    await settle(hass)
    assert result["entities"][A] == "queued"
    assert hass.states.get(A).state == "off"
    assert engine.records[A].diverged is None


async def test_startup_sends_no_light_command_until_something_calls_layers(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any]
) -> None:
    """SPEC 7.3: never at startup otherwise, never because of an external change."""
    now = now_ts()
    seed(hass_storage, {
        A: Record(A, base=BASE_A,
                  layers={"hold": make_layer("hold", 40, OFF_COMMAND, seq=1, set_at=now - 900)},
                  observed=shows(hass, A)),
        C: Record(C, base=OFF_COMMAND,
                  layers={"nightlight": make_layer("nightlight", 50, Command(ON, 13), seq=2,
                                                   set_at=now - 900)},
                  observed=shows(hass, C)),
        D: Record(D, base=OFF_COMMAND,
                  layers={"signal": make_layer("signal", 70, Command(ON, 255, Color.kelvin(6500)),
                                               seq=3, set_at=now - 900)},
                  observed=shows(hass, D)),
    })
    calls = watch_light_calls(hass)
    entry = await setup_layers(hass, [A, B, C, D], apply=False, end_grace=False)
    engine = engine_of(entry)

    await advance(hass, freezer, 2)
    lights["a"].push(brightness=104)                   # re-reports during the grace: what
    lights["c"].push(is_on=False)                      # they showed when Layers stopped
    await advance(hass, freezer, 4)                    # the grace ends
    assert not engine.in_grace
    lights["d"].push(is_on=True, brightness=128)       # a no-context change after it
    await advance(hass, freezer, 5)
    lights["a"].set_available(False)                   # a layered lamp drops and comes back
    await hass.async_block_till_done()
    lights["a"].set_available(True)
    await hass.async_block_till_done()
    await advance(hass, freezer, RETURN_SETTLE_S + 0.5)
    for _ in range(4):
        await advance(hass, freezer, 150)
    await layers(hass, "get", {})

    assert calls == []
    assert all(lamp.calls == [] for lamp in lights.values())

    await layers(hass, "sync", {})
    await settle(hass)
    assert "turn_off" in services(calls)               # the hold on lamp_a is pushed now
    assert hass.states.get(A).state == "off"


async def test_the_grace_runs_from_home_assistant_started_when_set_up_during_startup(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any]
) -> None:
    now = now_ts()
    seed(hass_storage, {
        A: Record(A, base=BASE_A,
                  layers={"hold": make_layer("hold", 40, OFF_COMMAND, seq=1, set_at=now - 900)},
                  observed=shows(hass, A)),
    })
    calls = watch_light_calls(hass)
    hass.set_state(CoreState.starting)
    entry = await setup_layers(hass, [A], apply=False, end_grace=False)
    engine = engine_of(entry)

    await advance(hass, freezer, 60)                   # still starting: the grace has not begun
    assert engine.in_grace
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    await advance(hass, freezer, STARTUP_GRACE_S - 10)
    assert engine.in_grace
    await advance(hass, freezer, 15)
    assert not engine.in_grace
    assert engine.records[A].diverged == DIV_UNSYNCED
    assert calls == []


# =========================================================================== apply switch


async def test_a_fresh_install_starts_with_apply_off(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any]
) -> None:
    assert STORAGE_KEY not in hass_storage
    entry = await start(hass, freezer, [A], apply=False)
    engine = engine_of(entry)
    assert engine.apply is False
    assert hass.states.get(APPLY).state == "off"
    assert hass.states.get(STATUS).state == "shadow"
    calls = watch_light_calls(hass)
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off"})
    await settle(hass)
    assert calls == []


@pytest.mark.parametrize(
    ("store_apply", "cached", "expected"),
    [(None, "on", "off"), (False, "on", "off"), (True, "off", "on")],
    ids=["fresh_store_cache_on", "store_off_cache_on", "store_on_cache_off"],
)
async def test_apply_comes_from_the_store_not_the_restore_cache(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_storage: dict[str, Any],
    store_apply: bool | None, cached: str, expected: str,
) -> None:
    if store_apply is not None:
        seed(hass_storage, {}, apply=store_apply)
    mock_restore_cache(hass, [State(APPLY, cached)])
    entry = await setup_layers(hass, [A], apply=False, end_grace=False)
    await hass.async_block_till_done()
    assert hass.states.get(APPLY).state == expected
    assert engine_of(entry).apply is (expected == "on")


async def test_the_apply_switch_is_saved_immediately(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_storage: dict[str, Any]
) -> None:
    entry = await setup_layers(hass, [A], apply=False, end_grace=False)
    await hass.services.async_call("switch", "turn_on", {"entity_id": APPLY}, blocking=True)
    assert stored(hass_storage)["apply"] is True           # no delay: written by the call itself
    assert hass.states.get(APPLY).state == "on"
    await hass.services.async_call("switch", "turn_off", {"entity_id": APPLY}, blocking=True)
    assert stored(hass_storage)["apply"] is False
    assert engine_of(entry).apply is False


# =========================================================================== reload


async def test_reload_keeps_layers_tombstones_and_apply_and_sends_nothing(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_admin_user: MockUser
) -> None:
    entry = await start(hass, freezer, [A, B])
    engine = engine_of(entry)
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off",
                               "ttl": 3600, "owner": "tv", "resume_after_manual": True,
                               "on_expire": "render"})
    await layers(hass, "set", {"entity_id": B, "layer": "signal", "priority": 70, "brightness": 50,
                               "ttl": 3600, "resume_after_manual": True})
    await settle(hass)
    await hass.services.async_call("light", "turn_on", {"entity_id": B, "brightness": 230},
                                   blocking=True, context=Context(user_id=hass_admin_user.id))
    await settle(hass)
    assert "signal" in engine.records[B].tombstones
    before = {eid: engine.records[eid].to_json() for eid in (A, B)}
    seq = engine.seq
    calls = watch_light_calls(hass)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    fresh = engine_of(entry)
    assert fresh is not engine
    assert fresh.apply is True and hass.states.get(APPLY).state == "on"
    assert fresh.seq == seq
    for eid in (A, B):
        after = fresh.records[eid].to_json()
        for key in ("base", "layers", "tombstones"):
            assert after[key] == before[eid][key], (eid, key)

    await advance(hass, freezer, RETURN_SETTLE_S + 0.5)
    assert calls == []
    assert fresh.records[A].diverged is None                # it shows its hold

    await layers(hass, "set", {"entity_id": A, "layer": "other", "priority": 60, "state": "off"})
    assert fresh.records[A].layers["other"].seq == seq + 1


async def test_a_ttl_that_runs_out_during_the_reload_grace_waits_for_its_end(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any
) -> None:
    entry = await start(hass, freezer, [A])
    await layers(hass, "set", {"entity_id": A, "layer": "signal", "priority": 70, "brightness": 255,
                               "xy_color": list(SIGNAL_XY), "ttl": 3})
    await settle(hass)
    assert brightness(hass, A) == 255
    calls = watch_light_calls(hass)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    engine = engine_of(entry)

    await advance(hass, freezer, 3.5)
    assert engine.in_grace
    assert "signal" in engine.records[A].layers           # ran out, held for the grace end
    assert brightness(hass, A) == 255
    assert calls == []

    await advance(hass, freezer, 2)
    assert not engine.in_grace
    assert engine.records[A].layers == {}
    assert_shows_base_a(hass)
    assert services(calls) == ["turn_on"]


async def test_a_render_cut_off_by_a_reload_is_still_owed_and_resumes_after_the_grace(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await start(hass, freezer, [A])
    monkeypatch.setattr(render_module, "RETRY_BACKOFF_S", (30.0,))   # park the retry
    lights["a"].ignore_commands = True
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off"})
    old = engine_of(entry)
    assert old.renderer.alive(A)
    assert old.records[A].owed is not None

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert not old.renderer.alive(A)
    engine = engine_of(entry)
    assert engine.records[A].owed is not None                     # persisted at the render's start
    sent = len(lights["a"].calls)
    lights["a"].ignore_commands = False

    await advance(hass, freezer, RETURN_SETTLE_S - 1)
    assert len(lights["a"].calls) == sent                         # nothing inside the grace
    await advance(hass, freezer, 1.5)
    assert not engine.in_grace
    assert hass.states.get(A).state == "off"
    assert engine.records[A].owed is None


async def test_a_late_report_under_our_context_right_after_a_reload_is_not_a_take_back(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any
) -> None:
    """SPEC 7.2: our contexts stay recognisable for at least 5 s after a render, because
    Home Assistant stamps the lamp's writes with them for 5 s. A reload inside that
    window (an options change) must not turn our own render into an external change."""
    entry = await start(hass, freezer, [A])
    lamp = lights["a"]
    await layers(hass, "set", {"entity_id": A, "layer": "dim", "priority": 40, "brightness": 50})
    await settle(hass)
    ours = hass.states.get(A).context
    assert ours.parent_id is not None and brightness(hass, A) == 50

    # Control: a late re-report inside the 5 s reuse is recognised as ours.
    lamp._attr_brightness = 51
    lamp.async_write_ha_state()
    await hass.async_block_till_done()
    assert hass.states.get(A).context.id == ours.id
    assert "dim" in engine_of(entry).records[A].layers

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    lamp._attr_brightness = 52
    lamp.async_write_ha_state()
    await hass.async_block_till_done()
    assert hass.states.get(A).context.id == ours.id        # still inside Home Assistant's reuse

    rec = engine_of(entry).records[A]
    assert "dim" in rec.layers
    assert "dim" not in rec.tombstones


# =========================================================================== removal


@pytest.mark.parametrize("render_in_flight", [False, True], ids=["idle", "render_in_flight"])
async def test_removing_the_entry_deletes_its_store_and_a_reinstall_starts_clean(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch, render_in_flight: bool,
) -> None:
    entry = await start(hass, freezer, [A])
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off"})
    await advance(hass, freezer, 3)
    assert A in stored(hass_storage)["entities"]
    if render_in_flight:
        monkeypatch.setattr(render_module, "RETRY_BACKOFF_S", (30.0,))
        lights["a"].ignore_commands = True
        await layers(hass, "set", {"entity_id": A, "layer": "signal", "priority": 70, "brightness": 255})
        assert engine_of(entry).renderer.alive(A)

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert STORAGE_KEY not in hass_storage

    await advance(hass, freezer, 120)                      # past any delayed or lazy save
    assert STORAGE_KEY not in hass_storage

    lights["a"].ignore_commands = False
    again = await setup_layers(hass, [A], apply=False, end_grace=False)
    engine = engine_of(again)
    assert engine.apply is False
    assert engine.records[A].layers == {} and engine.records[A].tombstones == {}


# =========================================================================== shutdown


async def test_stopping_home_assistant_mid_render_leaves_no_timers_or_tasks(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await start(hass, freezer, [A, B, C, D])
    engine = engine_of(entry)
    # Something pending of every kind: a TTL, released contexts, a debounce, a return, a render.
    await layers(hass, "set", {"entity_id": C, "layer": "nightlight", "priority": 50, "brightness": 13,
                               "ttl": 600})
    await settle(hass)
    lights["b"].push(brightness=10)
    lights["d"].set_available(False)
    lights["d"].set_available(True)
    await hass.async_block_till_done()
    assert engine._pending("debounce", B) and engine._pending("return", D)   # noqa: SLF001
    monkeypatch.setattr(render_module, "RETRY_BACKOFF_S", (30.0,))
    lights["a"].ignore_commands = True
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off"})
    assert engine.renderer.alive(A)

    await hass.async_stop()

    assert not engine.renderer.alive(A)
    assert engine._timers == {} and engine._releases == {}               # noqa: SLF001
    assert engine._ttl_unsub is None                                     # noqa: SLF001
    assert engine.renderer._late == {}                                   # noqa: SLF001
    assert layers_timers(hass) == []
    import asyncio

    renders = [t for t in asyncio.all_tasks() if t.get_name().startswith(f"{DOMAIN} render") and not t.done()]
    assert renders == []
    # The render that was cut off is still owed after the final write.
    saved = stored(hass_storage)["entities"]
    assert saved[A]["owed"] is not None
    assert "nightlight" in saved[C]["layers"]


# =========================================================================== the Store


async def test_only_records_worth_persisting_are_stored(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any]
) -> None:
    entry = await start(hass, freezer, [A, B, C])
    engine = engine_of(entry)
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off"})
    await layers(hass, "set", {"entity_id": B, "layer": "base", "brightness": 60})
    await settle(hass)
    await advance(hass, freezer, 3)

    data = stored(hass_storage)
    assert set(data["entities"]) == {A}
    assert data["apply"] is True and data["seq"] == engine.seq
    assert engine.records[B].base == Command(ON, 60)        # kept in memory only
    assert engine.records[C].base == OFF_COMMAND

    await layers(hass, "clear", {"entity_id": A, "layer": "hold"})
    await settle(hass)
    await advance(hass, freezer, 3)
    assert stored(hass_storage)["entities"] == {}

    # Across a reload the in-memory base is gone and re-learned from what the lamp shows.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    fresh = engine_of(entry)
    assert fresh.records[B].base is None
    await advance(hass, freezer, RETURN_SETTLE_S + 0.5)
    assert fresh.records[B].base == Command(ON, 60)
    assert fresh.records[A].base == BASE_A


async def test_a_newer_minor_version_is_passed_through_unchanged(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any]
) -> None:
    """Rolling back to an older release must keep the lamps' layers (store.py)."""
    now = now_ts()
    record = Record(A, base=BASE_A, observed=shows(hass, A),
                    layers={"hold": make_layer("hold", 40, OFF_COMMAND, seq=7, set_at=now - 60)})
    data = {
        "seq": 7,
        "apply": True,
        "entities": {A: {**record.to_json(), "added_later": {"x": 1}}},
        "added_later": [1, 2],
    }
    assert await LayersStore(hass)._async_migrate_func(1, 2, deepcopy(data)) == data   # noqa: SLF001

    hass_storage[STORAGE_KEY] = {"version": 1, "minor_version": 2, "key": STORAGE_KEY,
                                 "data": deepcopy(data)}
    entry = await setup_layers(hass, [A], apply=False, end_grace=False)
    engine = engine_of(entry)
    assert engine.apply is True and engine.seq == 7
    assert engine.records[A].layers["hold"].to_json() == record.layers["hold"].to_json()
    assert stored(hass_storage) == data                    # Home Assistant re-saved it as it was
