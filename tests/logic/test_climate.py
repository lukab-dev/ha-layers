"""Climate: the model, the four answers in SPEC 8, and the manual layer."""

from __future__ import annotations

import pytest
from layers_logic import climate as cc
from layers_logic.model import (
    DIV_DELIVERY,
    DIV_UNSYNCED,
    ON_EXPIRE_RENDER,
    SCOPE_LAMP,
    SCOPE_ROOM,
    SRC_AUTOMATION,
    SRC_DEVICE,
    SRC_USER,
    STRENGTH_LOCKED,
    STRENGTH_STICKY,
)
from layers_logic.policy import PolicyError

EID = "climate.zone"
NOW = 1_000_000.0
CAPS = cc.ClimateCaps(hvac_modes=frozenset({"off", "heat"}),
                      preset_modes=frozenset({"eco", "comfort", "away"}),
                      min_temp=5.0, max_temp=30.0, step=None, setpoint=True, presets=True)
MANUAL = 80


def heat(t: float | None = None, preset: str | None = None) -> cc.ClimateCommand:
    return cc.ClimateCommand("heat", t, preset)


OFF = cc.ClimateCommand("off")


def obs(state: str = "heat", t: float | None = 21.0, preset: str | None = None,
        current: float | None = 20.0, at: float = NOW) -> cc.ClimateObserved:
    return cc.ClimateObserved(state, t, preset, current, "idle", at)


class Seq:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> int:
        self.n += 1
        return self.n


def rec_with(base: cc.ClimateCommand | None = None, **layers: cc.ClimateLayer) -> cc.ClimateRecord:
    rec = cc.ClimateRecord(EID, base=base)
    rec.layers = dict(layers)
    return rec


def layer(id_: str, priority: int, command: cc.ClimateCommand, *, mode: str = "set",
          seq: int = 1, expires_at: float | None = None, strength: str = "soft",
          on_expire: str = "safe") -> cc.ClimateLayer:
    return cc.ClimateLayer(id_, priority, mode, command, seq, NOW - 100, expires_at,
                           on_expire=on_expire, strength=strength)


def setreq(layer_id: str, command: cc.ClimateCommand, **kw) -> cc.ClimateSetRequest:
    return cc.ClimateSetRequest(layer=layer_id, command=command, **kw)


# --------------------------------------------------------------------------- what a layer holds


def test_a_set_layer_replaces_the_target_and_keeps_the_mode_it_does_not_name() -> None:
    rec = rec_with(heat(21), away=layer("away", 50, cc.ClimateCommand(temperature=17)))
    assert cc.resolve(rec, NOW) == (heat(17), "away")


def test_off_keeps_the_target_below_for_the_layer_above_it() -> None:
    rec = rec_with(heat(21), window=layer("window", 90, OFF))
    assert cc.resolve(rec, NOW)[0] == cc.ClimateCommand("off", 21.0)
    assert cc.project(cc.resolve(rec, NOW)[0], CAPS) == (
        cc.ClimateCall.make("set_hvac_mode", hvac_mode="off"),)


def test_a_layer_without_a_mode_never_turns_heating_on() -> None:
    rec = rec_with(OFF, warm=layer("warm", 50, cc.ClimateCommand(temperature=23)))
    effective = cc.resolve(rec, NOW)[0]
    assert effective.hvac_mode == "off"
    assert cc.project(effective, CAPS) == (cc.ClimateCall.make("set_hvac_mode", hvac_mode="off"),)


def test_a_preset_replaces_the_temperature_below_and_the_other_way_round() -> None:
    rec = rec_with(heat(21), eco=layer("eco", 30, heat(preset="eco")))
    assert cc.resolve(rec, NOW)[0] == heat(None, "eco")
    rec.layers["boost"] = layer("boost", 60, cc.ClimateCommand(temperature=24), seq=2)
    assert cc.resolve(rec, NOW)[0] == heat(24)


def test_adjust_offsets_stack_and_only_shift_a_thermostat_that_is_on() -> None:
    rec = rec_with(heat(21),
                   away=layer("away", 40, cc.ClimateCommand(offset=-2), mode="adjust"),
                   night=layer("night", 30, cc.ClimateCommand(offset=-1), mode="adjust", seq=2))
    assert cc.resolve(rec, NOW) == (heat(18), "away")
    rec.base = OFF
    assert cc.resolve(rec, NOW) == (OFF, "base")


def test_adjust_over_a_preset_without_a_temperature_does_nothing() -> None:
    rec = rec_with(heat(None, "eco"), away=layer("away", 40, cc.ClimateCommand(offset=-2),
                                                  mode="adjust"))
    assert cc.resolve(rec, NOW) == (heat(None, "eco"), "base")


# --------------------------------------------------------------------------- projecting


def test_project_sends_mode_then_preset_then_temperature() -> None:
    calls = cc.project(heat(20.5, "comfort"), CAPS)
    assert [c.service for c in calls] == ["set_hvac_mode", "set_preset_mode", "set_temperature"]


def test_project_rounds_to_the_step_and_clamps_to_the_range() -> None:
    caps = cc.ClimateCaps(hvac_modes=frozenset({"heat"}), min_temp=7, max_temp=25, step=0.5,
                          setpoint=True)
    assert cc.project(heat(21.3), caps)[-1].as_dict() == {"temperature": 21.5}
    assert cc.project(heat(40), caps)[-1].as_dict() == {"temperature": 25}


def test_project_drops_what_the_thermostat_does_not_take() -> None:
    caps = cc.ClimateCaps(hvac_modes=frozenset({"off", "heat"}), setpoint=True)
    assert cc.project(cc.ClimateCommand("cool", 21, "eco"), caps) == (
        cc.ClimateCall.make("set_temperature", temperature=21.0),)


# --------------------------------------------------------------------------- checking it arrived


def test_a_setpoint_that_reads_back_rounded_counts_as_taken() -> None:
    calls = cc.project(heat(21.3), CAPS)
    assert cc.matches(obs(t=21.5), calls, CAPS)
    assert cc.matches(obs(t=21.0), cc.project(heat(21.2), CAPS), CAPS)
    assert not cc.matches(obs(t=22.0), calls, CAPS)


def test_one_click_on_a_dial_is_not_inside_the_tolerance() -> None:
    assert not cc.close(obs(t=21.0), obs(t=21.5), CAPS)
    assert cc.close(obs(t=21.0, current=19.0), obs(t=21.0, current=21.4), CAPS)


def test_a_coarse_step_widens_the_tolerance() -> None:
    caps = cc.ClimateCaps(hvac_modes=frozenset({"heat"}), step=1.0, setpoint=True)
    assert cc.tolerance(caps) == 0.5
    assert cc.tolerance(CAPS) == cc.TOL_SETPOINT


def test_both_mode_and_setpoint_are_checked() -> None:
    calls = cc.project(heat(21), CAPS)
    assert not cc.matches(obs("off", None), calls, CAPS)
    assert not cc.matches(obs("heat", 18), calls, CAPS)
    assert not cc.matches(obs("heat", 21, preset="eco"), cc.project(heat(None, "comfort"), CAPS), CAPS)
    assert cc.matches(obs("off", None), cc.project(OFF, CAPS), CAPS)


# --------------------------------------------------------------------------- the safety rule


def test_starts_is_only_off_to_on() -> None:
    assert cc.starts(obs("off", None), heat(21))
    assert cc.starts(None, heat(21))
    assert cc.starts(obs("unavailable", None), heat(21))
    assert not cc.starts(obs("heat", 17), heat(23))     # a setpoint move is not a start
    assert not cc.starts(obs("heat", 21), OFF)
    assert not cc.starts(obs("off", None), cc.ClimateCommand(temperature=21))


def test_an_expiry_never_starts_heating_and_the_base_learns_off() -> None:
    rec = rec_with(heat(21), window=layer("window", 90, OFF, expires_at=NOW - 1,
                                          strength=STRENGTH_LOCKED))
    res = cc.expire(rec, NOW, obs("off", None))
    assert res.expired == ("window",) and not res.render
    assert rec.base == OFF and rec.base_source == "expiry"


def test_an_expiry_may_move_a_setpoint_back_either_way() -> None:
    for held in (24.0, 17.0):
        rec = rec_with(heat(21), manual=layer("manual", MANUAL, heat(held), expires_at=NOW - 1))
        res = cc.expire(rec, NOW, obs("heat", held))
        assert res.render, held
        assert cc.resolve(rec, NOW)[0] == heat(21)


def test_an_expiry_with_a_layer_still_holding_the_mode_sends_nothing_either() -> None:
    """Stricter than lamps: a holiday layer under an expired window layer does not
    switch the heating back on by itself."""
    rec = rec_with(heat(21),
                   holiday=layer("holiday", 60, heat(16)),
                   window=layer("window", 90, OFF, seq=2, expires_at=NOW - 1))
    res = cc.expire(rec, NOW, obs("off", None))
    assert not res.render
    assert rec.base == heat(21) and rec.diverged == DIV_UNSYNCED


def test_on_expire_render_or_a_clear_after_the_end_restores() -> None:
    rec = rec_with(heat(21), window=layer("window", 90, OFF, expires_at=NOW - 1,
                                          on_expire=ON_EXPIRE_RENDER))
    assert cc.expire(rec, NOW, obs("off", None)).render
    rec = rec_with(heat(21), window=layer("window", 90, OFF, expires_at=NOW - 1))
    assert cc.apply_clear(rec, "window", NOW).expired == ("window",)
    assert cc.expire(rec, NOW, obs("off", None)).render


# --------------------------------------------------------------------------- the manual layer


def ext(rec: cc.ClimateRecord, shown: cc.ClimateCommand, *, source: str = SRC_DEVICE,
        scope: str = SCOPE_LAMP, timeout: float | None = 3 * 3600) -> cc.ExternalResult:
    return cc.apply_external(rec, shown, source, NOW, Seq(), manual_priority=MANUAL,
                             manual_timeout=timeout, caps=CAPS, scope=scope)


def test_a_persons_change_outranks_holiday_and_away_and_runs_out() -> None:
    rec = rec_with(heat(21),
                   holiday=layer("holiday", 60, heat(16)),
                   away=layer("away", 50, cc.ClimateCommand(offset=-3), mode="adjust", seq=2))
    rec.observed = obs("heat", 22)
    res = ext(rec, heat(22))
    assert res.edited == "manual" and not res.dropped and not res.held
    manual = rec.layers["manual"]
    assert manual.priority == MANUAL and manual.expires_at == NOW + 3 * 3600
    assert set(rec.layers) == {"holiday", "away", "manual"}
    assert rec.base == heat(21), "the schedule's base never changes"
    assert cc.resolve(rec, NOW) == (heat(22), "manual")
    # three hours later the holiday shows again
    cc.expire(rec, NOW + 3 * 3600, obs("heat", 22))
    assert cc.resolve(rec, NOW + 3 * 3600) == (heat(16), "holiday")


def test_an_open_window_wins_and_the_persons_setting_comes_back_on_close() -> None:
    rec = rec_with(heat(21), window=layer("window", 90, OFF, strength=STRENGTH_LOCKED))
    rec.observed = obs("heat", 23)          # someone turned the dial while it was open
    res = ext(rec, heat(23))
    assert res.held == ("window",)
    assert rec.diverged == DIV_DELIVERY     # the engine shows the window layer again
    assert cc.resolve(rec, NOW) == (cc.ClimateCommand("off", 23.0), "window")
    cc.apply_clear(rec, "window", NOW + 600)
    assert cc.resolve(rec, NOW + 600) == (heat(23), "manual")


def test_manual_above_the_window_is_a_priority_choice() -> None:
    rec = rec_with(heat(21), window=layer("window", 70, OFF, strength=STRENGTH_LOCKED))
    rec.observed = obs("heat", 23)
    res = ext(rec, heat(23))
    assert not res.held
    assert cc.resolve(rec, NOW) == (heat(23), "manual")


def test_soft_layers_at_or_above_the_manual_priority_are_taken_back() -> None:
    rec = rec_with(heat(21), boost=layer("boost", 85, heat(25)))
    rec.observed = obs("heat", 19)
    res = ext(rec, heat(19))
    assert res.dropped == ("boost",) and "boost" in rec.tombstones
    assert cc.apply_set(rec, setreq("boost", heat(25), priority=85), NOW, Seq(), MANUAL).result \
        == "skipped_tombstoned"


def test_a_sticky_layer_above_goes_at_the_thermostat_and_holds_against_an_automation() -> None:
    def fresh() -> cc.ClimateRecord:
        rec = rec_with(heat(21), alert=layer("alert", 95, heat(15), strength=STRENGTH_STICKY))
        rec.observed = obs("heat", 22)
        return rec

    rec = fresh()
    assert ext(rec, heat(22), scope=SCOPE_LAMP).dropped == ("alert",)
    rec = fresh()
    res = ext(rec, heat(22), source=SRC_AUTOMATION, scope=SCOPE_ROOM)
    assert res.held == ("alert",) and not res.dropped


def test_a_second_change_replaces_the_manual_layer_and_restarts_its_time() -> None:
    rec = rec_with(heat(21))
    rec.observed = obs("heat", 22)
    ext(rec, heat(22))
    rec.layers["manual"].expires_at = NOW + 60
    ext(rec, OFF)
    assert rec.layers["manual"].command == OFF
    assert rec.layers["manual"].expires_at == NOW + 3 * 3600


def test_manual_timeout_none_holds_until_cleared() -> None:
    rec = rec_with(heat(21))
    ext(rec, heat(22), timeout=None)
    assert rec.layers["manual"].expires_at is None
    cc.apply_clear(rec, "manual", NOW)
    assert cc.resolve(rec, NOW) == (heat(21), "base")


# --------------------------------------------------------------------------- layers.set rules


@pytest.mark.parametrize(("req", "reason"), [
    (setreq("manual", heat(21), priority=50), "'manual'"),
    (setreq("night", heat(18), priority=MANUAL), "manual priority"),
    (setreq("away", cc.ClimateCommand(offset=-2), priority=40), "mode: adjust"),
    (setreq("away", heat(18), priority=40, mode="adjust"), "offset only"),
    (setreq("away", cc.ClimateCommand("off", 18), priority=40), "takes no temperature"),
    (setreq("x", cc.ClimateCommand(), priority=40), "give an hvac_mode"),
    (setreq("x", heat(18), priority=40, mode="follow"), "does not apply"),
    (setreq("base", heat(18), strength="sticky"), "strength"),
])
def test_set_refuses(req: cc.ClimateSetRequest, reason: str) -> None:
    with pytest.raises(PolicyError) as err:
        cc.apply_set(rec_with(heat(21)), req, NOW, Seq(), MANUAL)
    assert reason in str(err.value)


def test_set_base_create_refresh_update_and_priority_rules() -> None:
    rec = rec_with(None)
    seq = Seq()
    assert cc.apply_set(rec, setreq("base", heat(21)), NOW, seq, MANUAL).result == "base_set"
    assert rec.base == heat(21)
    assert cc.apply_set(rec, setreq("night", heat(18), priority=40), NOW, seq, MANUAL).result == "created"
    assert cc.apply_set(rec, setreq("night", heat(18), expires_at=NOW + 60), NOW, seq,
                        MANUAL).result == "refreshed"
    assert rec.layers["night"].expires_at == NOW + 60
    assert cc.apply_set(rec, setreq("night", heat(17)), NOW, seq, MANUAL).result == "updated"
    with pytest.raises(PolicyError) as err:
        cc.apply_set(rec, setreq("away", heat(16), priority=40), NOW, seq, MANUAL)
    assert err.value.code == "priority_conflict"
    with pytest.raises(PolicyError) as err:
        cc.apply_set(rec, setreq("away", heat(16)), NOW, seq, MANUAL)
    assert err.value.code == "priority_required"


def test_clear_all_leaves_strong_layers() -> None:
    rec = rec_with(heat(21), window=layer("window", 90, OFF, strength=STRENGTH_LOCKED),
                   night=layer("night", 30, heat(18), seq=2))
    res = cc.apply_clear(rec, "all", NOW)
    assert res.removed == ("night",) and res.kept == ("window",)


# --------------------------------------------------------------------------- telling a person from a slow device


def ev(old: cc.ClimateObserved, new: cc.ClimateObserved, *, ctx: str | None = None,
       user: str | None = None, parent: str | None = None) -> cc.ClimateEvent:
    return cc.ClimateEvent(EID, old, new, ctx, parent, user, NOW)


def ours_rec(at: float, matched_at: float | None = None) -> cc.ClimateRecord:
    rec = rec_with(heat(21), night=layer("night", 30, heat(18)))
    rec.last_command = cc.ClimateLastCommand(at, True, heat(18), "ours1", matched_at)
    return rec


def test_room_temperature_reports_are_not_changes() -> None:
    v = cc.classify(rec_with(heat(21)), ev(obs(current=20.1), obs(current=20.4)),
                    cc.ClimateRuntime(), CAPS, NOW)
    assert v.kind == cc.MEASUREMENT


def test_our_context_landing_is_ours_even_in_two_reports() -> None:
    rt = cc.ClimateRuntime(ours={"ours1": heat(18)})
    rec = ours_rec(NOW - 2)
    assert cc.classify(rec, ev(obs("off", None), obs("heat", 21), ctx="ours1"), rt, CAPS,
                       NOW).kind == cc.OURS                 # the mode first, setpoint not yet
    assert cc.classify(rec, ev(obs(t=21), obs(t=18), ctx="ours1"), rt, CAPS, NOW).kind == cc.OURS
    # our context on a write that goes somewhere else is not ours
    assert cc.classify(rec, ev(obs(t=21), obs(t=25), ctx="ours1"), rt, CAPS,
                       NOW).kind == cc.LATE


def test_no_context_inside_the_window_is_the_device_until_it_has_arrived() -> None:
    rt = cc.ClimateRuntime(window=30)
    rec = ours_rec(NOW - 10)
    assert cc.classify(rec, ev(obs(t=21), obs(t=19)), rt, CAPS, NOW).kind == cc.LATE
    rec = ours_rec(NOW - 10, matched_at=NOW - 7)            # showed 18 for 7 s, then moved
    assert cc.classify(rec, ev(obs(t=18), obs(t=20)), rt, CAPS, NOW).kind == cc.DEBOUNCE
    rec = ours_rec(NOW - 31)
    assert cc.classify(rec, ev(obs(t=21), obs(t=19)), rt, CAPS, NOW).kind == cc.DEBOUNCE


def test_a_render_still_running_judges_its_own_reports() -> None:
    rt = cc.ClimateRuntime(render_alive=True)
    assert cc.classify(ours_rec(NOW - 1), ev(obs(t=21), obs(t=19)), rt, CAPS, NOW).kind == cc.NOISE


def test_context_says_who() -> None:
    rec = rec_with(heat(21))
    rt = cc.ClimateRuntime(calls={"c1": (SRC_AUTOMATION, None)})
    assert cc.classify(rec, ev(obs(t=21), obs(t=22), user="u"), rt, CAPS, NOW) == \
        cc.ClimateVerdict(cc.EXTERNAL, SRC_USER, "u")
    assert cc.classify(rec, ev(obs(t=21), obs(t=22), ctx="c1"), rt, CAPS, NOW).source == SRC_AUTOMATION
    assert cc.classify(rec, ev(obs(t=21), obs(t=22), parent="p"), rt, CAPS, NOW).source == SRC_AUTOMATION


def test_settle_rereport_or_a_person() -> None:
    rec = rec_with(heat(21))
    rec.observed_prev = obs(t=21)
    assert cc.settle(rec, obs(t=21, current=19), CAPS, NOW).kind == cc.REREPORT
    assert cc.settle(rec, obs(t=23), CAPS, NOW) == cc.ClimateVerdict(cc.EXTERNAL, SRC_DEVICE)


def test_scope() -> None:
    assert cc.change_scope(SRC_DEVICE) == SCOPE_LAMP
    assert cc.change_scope(SRC_USER) == SCOPE_LAMP
    assert cc.change_scope(SRC_AUTOMATION) == SCOPE_ROOM


# --------------------------------------------------------------------------- returns, attributes, json


def test_return_untouched_sends_changed_is_a_person() -> None:
    rec = rec_with(heat(21), night=layer("night", 30, heat(18)))
    rec.p_at_drop = obs(t=21)
    assert cc.decide_return(rec, obs(t=21), CAPS, NOW) == cc.SEND
    assert cc.decide_return(rec, obs(t=18), CAPS, NOW) == cc.NOTHING
    assert cc.decide_return(rec, obs(t=24), CAPS, NOW) == cc.EXTERNAL


def test_return_never_starts_heating_on_an_old_debt_without_evidence() -> None:
    rec = rec_with(heat(21))
    rec.owed = cc.ClimateOwed(NOW - 3600, heat(21))
    assert cc.decide_return(rec, obs("off", None), CAPS, NOW) == cc.RECORD


def test_attributes() -> None:
    attrs = {"hvac_modes": ["off", "heat"], "preset_modes": ["eco", "none"], "min_temp": 7,
             "max_temp": 30, "target_temp_step": 0.5, "supported_features": 1 | 16 | 128,
             "temperature": 21.5, "preset_mode": "none", "current_temperature": 20.3,
             "hvac_action": "heating"}
    caps = cc.caps_from_attrs(attrs)
    assert caps.setpoint and caps.presets and caps.step == 0.5 and "heat" in caps.hvac_modes
    o = cc.observed_from_state("heat", attrs, NOW)
    assert (o.temperature, o.preset, o.current, o.action) == (21.5, None, 20.3, "heating")
    assert cc.observed_to_command(o) == heat(21.5)
    assert cc.observed_to_command(cc.observed_from_state("off", attrs, NOW)) == OFF


def test_record_json_round_trip() -> None:
    rec = rec_with(heat(21), away=layer("away", 40, cc.ClimateCommand(offset=-2), mode="adjust",
                                        expires_at=NOW + 60, strength=STRENGTH_STICKY))
    rec.observed = obs()
    rec.owed = cc.ClimateOwed(NOW, heat(19))
    rec.diverged = DIV_DELIVERY
    back = cc.ClimateRecord.from_json(EID, rec.to_json())
    assert back.to_json() == rec.to_json()
    assert back.layers["away"].command.offset == -2
