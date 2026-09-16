"""The classifier (SPEC section 6), with incident replays on synthetic ids.

Times are seconds on a made-up clock; NOW is when the event is classified.
"""

from __future__ import annotations

import pytest

from layers_logic.classify import (
    DEBOUNCE,
    EXTERNAL,
    EXTERNAL_INTENT,
    FAILED_DELIVERY,
    FIRST,
    FOLLOW_UP,
    GONE,
    IGNORE,
    NOISE,
    NOTHING,
    OURS,
    RECORD,
    REREPORT,
    SEND,
    TRANSPORT_DOWN,
    TRANSPORT_UP,
    WAIT,
    CallInfo,
    OurCommand,
    ReturnDecision,
    Runtime,
    StateEvent,
    Verdict,
    classify_call,
    classify_state,
    decide_return,
    settle_debounce,
)
from layers_logic.model import (
    OFF_COMMAND,
    OWED_ON_MAX_AGE_S,
    Caps,
    Color,
    Command,
    External,
    LastCommand,
    Layer,
    Observed,
    Owed,
    Record,
)

NOW = 10_000.0
LAMP = "light.lamp_a"
OTHER = "light.lamp_b"

HUE = Caps(frozenset({"color_temp", "xy"}), 2200, 6500, transition=True, platform="hue")
MATTER = Caps(frozenset({"color_temp", "xy"}), 2000, 6500, transition=True, platform="matter")
DIMMER = Caps(frozenset({"brightness"}), platform="zha")

WARM = Color.kelvin(2700)


def obs(state: str, brightness: int | None = None, *, kelvin: int | None = None,
        xy: tuple[float, float] | None = None, at: float = NOW) -> Observed:
    mode = "color_temp" if kelvin is not None else "xy" if xy is not None else None
    if state == "on" and mode is None and brightness is not None:
        mode = "brightness"
    return Observed(state, brightness, mode, xy=xy, kelvin=kelvin, at=at)


ON_200 = obs("on", 200)
OFF = obs("off")
AWAY = obs("unavailable")


def event(old: Observed | None, new: Observed | None, *, ctx: str | None = None,
          parent: str | None = None, user: str | None = None, at: float = NOW) -> StateEvent:
    return StateEvent(LAMP, old, new, ctx, parent, user, at)


def call_info(ctx: str = "ctx-call", *, service: str = "turn_on", command: Command | None = None,
              groups: frozenset[str] | None = None, lamps: frozenset[str] = frozenset({LAMP}),
              via_room: frozenset[str] = frozenset(), user_id: str | None = None,
              first_seen: float = NOW - 1) -> CallInfo:
    if command is None and service == "turn_off":
        command = OFF_COMMAND
    if groups is None:
        groups = command.groups() if command is not None else frozenset()
    return CallInfo(ctx, "user" if user_id else "automation", user_id, service, command, groups,
                    lamps, via_room, first_seen)


def layer(layer_id: str, priority: int, command: Command, mode: str = "set", *, seq: int = 1,
          expires_at: float | None = None) -> Layer:
    return Layer(layer_id, priority, mode, command, command, seq, 0.0, expires_at)


def record(base: Command | None = None, *layers: Layer, **fields) -> Record:
    rec = Record(LAMP, base=base, **fields)
    for lay in layers:
        rec.layers[lay.id] = lay
    return rec


# --------------------------------------------------------------------------- #
# 6.1 Transport and first reports
# --------------------------------------------------------------------------- #


def test_gone():
    assert classify_state(record(), event(ON_200, None), Runtime(), HUE, NOW) == Verdict(GONE)


def test_transport_down_on_unavailable_or_unknown():
    for new in (AWAY, obs("unknown")):
        assert classify_state(record(), event(ON_200, new), Runtime(), HUE, NOW).kind == TRANSPORT_DOWN


def test_away_to_away_is_ignored():
    rt = Runtime()
    assert classify_state(record(), event(AWAY, obs("unknown")), rt, HUE, NOW) == Verdict(IGNORE)
    assert classify_state(record(), event(obs("unknown"), AWAY), rt, HUE, NOW) == Verdict(IGNORE)


def test_first_report_updates_observed_only():
    assert classify_state(record(), event(None, ON_200), Runtime(), HUE, NOW) == Verdict(FIRST)


def test_first_report_while_away_is_a_drop():
    # The entity appears unavailable (e.g. at startup): the engine freezes p_at_drop.
    assert classify_state(record(), event(None, AWAY), Runtime(), HUE, NOW).kind == TRANSPORT_DOWN


def test_transport_up_wins_over_any_context():
    rt = Runtime(ours={"ctx-ours": OurCommand("ctx-ours", Command("on", 200), NOW - 1)})
    ev = event(AWAY, ON_200, ctx="ctx-ours", parent="ctx-caller")
    assert classify_state(record(), ev, rt, HUE, NOW) == Verdict(TRANSPORT_UP)


# --------------------------------------------------------------------------- #
# 6.1 Ours
# --------------------------------------------------------------------------- #


def ours_rt(target: Command, *, alive: bool = True, ctx: str = "ctx-ours") -> Runtime:
    return Runtime(ours={ctx: OurCommand(ctx, target, NOW - 1)}, render_alive=alive)


def test_ours_when_the_state_agrees_with_our_command():
    ev = event(OFF, obs("on", 198), ctx="ctx-ours", parent="ctx-caller")
    assert classify_state(record(), ev, ours_rt(Command("on", 200)), HUE, NOW) == Verdict(OURS, "ours")


def test_ours_for_a_transition_step_towards_our_target():
    ev = event(obs("on", 200), obs("on", 150), ctx="ctx-ours", parent="ctx-caller")
    assert classify_state(record(), ev, ours_rt(Command("on", 64)), HUE, NOW).kind == OURS


def test_a_dimming_step_while_our_target_is_off_is_noise_not_ours():
    # SPEC 6.1: a step towards our target keeps its on/off state. On the way to
    # off the lamp is still on, so it is judged without context: our render's
    # verification handles it.
    ev = event(obs("on", 200), obs("on", 120), ctx="ctx-ours", parent="ctx-caller")
    assert classify_state(record(), ev, ours_rt(OFF_COMMAND), MATTER, NOW) == Verdict(NOISE)
    # Brighter while our target is off is not ours either.
    ev = event(obs("on", 100), obs("on", 200), ctx="ctx-ours", parent="ctx-caller")
    assert classify_state(record(), ev, ours_rt(OFF_COMMAND, alive=False), MATTER, NOW) == Verdict(DEBOUNCE)


def test_a_person_turning_the_lamp_up_inside_the_context_reuse_is_not_ours():
    # Motion turns a nightlight on at 13; 3 s later a person turns it up. HA stamps
    # the write with our context, but it moved away from our target: not ours.
    rec = record(OFF_COMMAND, layer("nl", 50, Command("on", 13)))
    rec.last_command = LastCommand(NOW - 3, ours=True, source="ours", target=Command("on", 13),
                                   context_id="ctx-ours")
    ev = event(obs("on", 13), obs("on", 254), ctx="ctx-ours", parent="ctx-caller")
    assert classify_state(rec, ev, ours_rt(Command("on", 13), alive=False), HUE, NOW) == Verdict(DEBOUNCE)


def test_ours_context_with_an_inconsistent_state_is_judged_without_context():
    # We turned the lamp off; inside HA's 5 s context reuse someone turns it on in a
    # vendor app. The write carries our context (with our caller as parent_id), yet
    # it must be neither OURS nor an automation's EXTERNAL.
    ev = event(OFF, obs("on", 255), ctx="ctx-ours", parent="ctx-caller")
    rec = record(Command("on", 200))
    assert classify_state(rec, ev, ours_rt(OFF_COMMAND, alive=False), MATTER, NOW) == Verdict(DEBOUNCE)
    assert classify_state(rec, ev, ours_rt(OFF_COMMAND, alive=True), MATTER, NOW) == Verdict(NOISE)
    # Our command is the last one sent. While our render runs, its verification
    # judges; once it is done, a flip this early is a person's, not a reversal.
    rec.last_command = LastCommand(NOW - 3, ours=True, source="ours", target=OFF_COMMAND,
                                   context_id="ctx-ours")
    assert classify_state(rec, ev, ours_rt(OFF_COMMAND), MATTER, NOW) == Verdict(NOISE)
    assert classify_state(rec, ev, ours_rt(OFF_COMMAND, alive=False), MATTER, NOW) == Verdict(DEBOUNCE)


def test_a_person_flipping_the_lamp_inside_the_context_reuse_is_not_retried():
    # Our nightlight verified at +1 s; at +3 s a person switches it off. The write
    # still carries our context. The measured bridge reversals come at 9.7-38 s
    # with no context: this one is a person's, and Layers must not fight it.
    rec = record(OFF_COMMAND, layer("nl", 50, Command("on", 13)))
    rec.last_command = LastCommand(NOW - 3, ours=True, source="ours", target=Command("on", 13),
                                   context_id="ctx-r1")
    ev = event(obs("on", 13), OFF, ctx="ctx-r1", parent="ctx-caller")
    rt = Runtime(ours={"ctx-r1": OurCommand("ctx-r1", Command("on", 13), NOW - 3)})
    assert classify_state(rec, ev, rt, HUE, NOW) == Verdict(DEBOUNCE)
    # The same with our context already released from Runtime.ours.
    assert classify_state(rec, ev, Runtime(), HUE, NOW) == Verdict(DEBOUNCE)
    # Without any context it is the late window's reversal, as measured.
    assert classify_state(rec, event(obs("on", 13), OFF), Runtime(), HUE, NOW).kind == FAILED_DELIVERY


def test_our_released_context_never_counts_as_someone_elses():
    # The engine dropped our context from Runtime.ours when the lamp first matched,
    # but HA keeps stamping writes with it for 5 s. It has a parent_id (our caller).
    rec = record(Command("on", 200))
    rec.last_command = LastCommand(NOW - 3, ours=True, source="ours", target=Command("on", 200),
                                   context_id="ctx-ours")
    ev = event(obs("on", 200), obs("on", 201), ctx="ctx-ours", parent="ctx-caller")
    assert classify_state(rec, ev, Runtime(), HUE, NOW) == Verdict(DEBOUNCE)


# --------------------------------------------------------------------------- #
# 6.1 External with a context
# --------------------------------------------------------------------------- #


def test_external_via_a_known_call_is_not_a_replay_when_newer_than_the_layers():
    call = call_info("ctx-rocker", command=Command("on", 102, WARM), first_seen=NOW - 1)
    rec = record(OFF_COMMAND, layer("tv", 40, OFF_COMMAND), last_layers_change=NOW - 100)
    ev = event(obs("on", 60, kelvin=2700), obs("on", 102, kelvin=2700), ctx="ctx-rocker")
    verdict = classify_state(rec, ev, Runtime(calls={"ctx-rocker": call}), HUE, NOW)
    assert verdict == Verdict(EXTERNAL, "automation", call, frozenset({"state", "brightness", "color"}))
    assert verdict.replay is False


def test_external_via_a_known_call_made_before_the_last_layers_change_is_a_replay():
    # A scene retry loop re-applies a press made 31 s ago; the tv layer arrived since.
    # The loop re-sends under the same context id; first_seen is when that id was
    # first seen (the engine keeps it across re-sends, beyond CALL_MEMORY_S).
    call = call_info("ctx-scene", command=Command("on", 102), first_seen=NOW - 31)
    rec = record(Command("on", 102), layer("tv", 40, Command(None, 64), "adjust"),
                 last_layers_change=NOW - 16)
    ev = event(obs("on", 64), obs("on", 102), ctx="ctx-scene")
    verdict = classify_state(rec, ev, Runtime(calls={"ctx-scene": call}), HUE, NOW)
    assert (verdict.kind, verdict.replay, verdict.call) == (EXTERNAL, True, call)


def test_a_call_first_seen_at_the_layers_change_is_no_replay():
    call = call_info("ctx-s", command=Command("on", 102), first_seen=NOW - 16)
    rec = record(Command("on", 102), layer("tv", 40, OFF_COMMAND), last_layers_change=NOW - 16)
    verdict = classify_state(rec, event(OFF, obs("on", 102), ctx="ctx-s"),
                             Runtime(calls={"ctx-s": call}), HUE, NOW)
    assert (verdict.kind, verdict.replay) == (EXTERNAL, False)


def test_known_call_is_no_replay_without_a_layers_change():
    call = call_info("ctx-a", command=Command("on", 50), first_seen=NOW - 31)
    ev = event(ON_200, obs("on", 50), ctx="ctx-a")
    verdict = classify_state(record(), ev, Runtime(calls={"ctx-a": call}), HUE, NOW)
    assert (verdict.kind, verdict.replay) == (EXTERNAL, False)


def test_known_call_by_a_user_keeps_its_source_and_groups():
    call = call_info("ctx-app", command=Command("on", 50), user_id="user-1")
    ev = event(ON_200, obs("on", 50), ctx="ctx-app", user="user-1")
    verdict = classify_state(record(), ev, Runtime(calls={"ctx-app": call}), HUE, NOW)
    assert (verdict.source, verdict.groups) == ("user", frozenset({"state", "brightness"}))


def test_known_call_that_did_not_target_the_lamp_takes_every_group():
    call = call_info("ctx-x", command=Command("on", 50), lamps=frozenset({OTHER}))
    ev = event(ON_200, obs("on", 50), ctx="ctx-x")
    verdict = classify_state(record(), ev, Runtime(calls={"ctx-x": call}), HUE, NOW)
    assert (verdict.kind, verdict.groups) == (EXTERNAL, None)


def test_known_toggle_takes_every_group():
    call = call_info("ctx-t", service="toggle", groups=frozenset({"state"}))
    verdict = classify_state(record(), event(OFF, ON_200, ctx="ctx-t"),
                             Runtime(calls={"ctx-t": call}), HUE, NOW)
    assert (verdict.kind, verdict.groups) == (EXTERNAL, None)


def test_known_call_with_an_unknown_intent_takes_every_group():
    # A brightness step: its intent cannot be known, so whatever the lamp shows is
    # taken, state included. Taking only brightness would leave the base off while
    # a person has the lamp on.
    call = call_info("ctx-step", command=None, groups=frozenset({"brightness"}), user_id="user-1")
    ev = event(OFF, obs("on", 26, kelvin=2700), ctx="ctx-step", user="user-1")
    verdict = classify_state(record(OFF_COMMAND), ev, Runtime(calls={"ctx-step": call}), HUE, NOW)
    assert (verdict.kind, verdict.source, verdict.groups) == (EXTERNAL, "user", None)


def test_known_turn_on_or_off_always_takes_the_state():
    call = call_info("ctx-b", command=Command("on", 50), groups=frozenset({"brightness"}))
    verdict = classify_state(record(Command("on", 200)), event(ON_200, obs("on", 50), ctx="ctx-b"),
                             Runtime(calls={"ctx-b": call}), HUE, NOW)
    assert verdict.groups == frozenset({"state", "brightness"})


def test_a_known_call_that_flips_the_lamp_takes_every_group_it_shows():
    # Apple Home "on" and Assist "turn on X" are bare turn_on calls: they name the
    # state only. But the lamp coming on decided its brightness and colour too. Taking
    # {state} alone would leave a base that is on with nothing else, which a later
    # clear cannot restore (the lamp stayed at the adjust layer's 25 %).
    bare = call_info("ctx-siri", command=Command("on"), groups=frozenset({"state"}))
    rt = Runtime(calls={"ctx-siri": bare})
    verdict = classify_state(record(OFF_COMMAND), event(OFF, obs("on", 200, kelvin=2700), ctx="ctx-siri"),
                             rt, HUE, NOW)
    assert verdict == Verdict(EXTERNAL, "automation", bare, None)
    # The same bare turn_on on a lamp that is already on changes nothing but the state.
    verdict = classify_state(record(Command("on", 200)), event(ON_200, obs("on", 200), ctx="ctx-siri"),
                             rt, HUE, NOW)
    assert verdict.groups == frozenset({"state"})
    # A flip off through a room call likewise takes everything (an off has nothing else).
    room = call_info("ctx-room", service="turn_off", lamps=frozenset({LAMP}), via_room=frozenset({LAMP}))
    verdict = classify_state(record(Command("on", 200)), event(ON_200, OFF),
                             Runtime(room_call=room), HUE, NOW)
    assert (verdict.kind, verdict.groups) == (EXTERNAL, None)


def test_external_via_user_id_not_in_the_call_map():
    ev = event(ON_200, obs("on", 30), ctx="ctx-unknown", user="user-1")
    assert classify_state(record(), ev, Runtime(), HUE, NOW) == Verdict(EXTERNAL, "user")


def test_external_via_parent_id_not_in_the_call_map():
    ev = event(ON_200, OFF, ctx="ctx-unknown", parent="ctx-automation")
    assert classify_state(record(), ev, Runtime(render_alive=True), HUE, NOW) == Verdict(
        EXTERNAL, "automation"
    )


def test_a_bare_context_is_no_context():
    # HomeKit and late entity writes carry a context id with no user and no parent.
    ev = event(ON_200, OFF, ctx="ctx-bare")
    assert classify_state(record(), ev, Runtime(), HUE, NOW) == Verdict(DEBOUNCE)


# --------------------------------------------------------------------------- #
# 6.1 No usable context
# --------------------------------------------------------------------------- #


def test_room_call_attributes_a_member_change_even_during_our_render():
    # "Turn off the room" through a vendor room group: members report with no
    # context. It is the person's, not noise for our render to fight.
    room = call_info("ctx-room", service="turn_off", user_id="user-1",
                     lamps=frozenset({LAMP, OTHER}), via_room=frozenset({LAMP, OTHER}))
    rec = record(Command("on", 200), layer("tv", 40, Command(None, 64), "adjust"))
    rec.last_command = LastCommand(NOW - 2, ours=True, source="ours", target=Command("on", 64))
    rt = Runtime(render_alive=True, room_call=room)
    verdict = classify_state(rec, event(obs("on", 64), OFF), rt, HUE, NOW)
    assert verdict == Verdict(EXTERNAL, "user", room, None)


def test_room_call_that_does_not_reach_the_lamp_is_ignored():
    room = call_info("ctx-room", service="turn_off", lamps=frozenset({OTHER}),
                     via_room=frozenset({OTHER}))
    rt = Runtime(room_call=room)
    assert classify_state(record(), event(ON_200, OFF), rt, HUE, NOW) == Verdict(DEBOUNCE)


def test_late_window_bridge_reverts_our_on_after_35_s():
    # Incident: our ON verified on the bridge's optimistic state; 35 s later the lamp
    # reports off with no context. Not a take-back: a failed delivery of ours.
    rec = record(OFF_COMMAND)
    rec.last_command = LastCommand(NOW - 35, ours=True, source="ours", target=Command("on", 200))
    verdict = classify_state(rec, event(ON_200, OFF), Runtime(), HUE, NOW)
    assert verdict == Verdict(FAILED_DELIVERY, "ours", ours=True, command=Command("on", 200))


def test_late_window_bridge_reverts_a_foreign_off_after_9_7_s():
    # Incident: a dimmer turned the lamp off; 9.7 s later the bridge relit it, no context.
    rec = record(OFF_COMMAND)
    rec.last_command = LastCommand(NOW - 9.7, ours=False, source="automation", target=OFF_COMMAND)
    verdict = classify_state(rec, event(OFF, obs("on", 13)), Runtime(), HUE, NOW)
    assert (verdict.kind, verdict.ours, verdict.command) == (FAILED_DELIVERY, False, OFF_COMMAND)


def test_the_same_reversal_after_20_s_on_another_platform_is_not_late():
    rec = record(OFF_COMMAND)
    rec.last_command = LastCommand(NOW - 20, ours=False, source="automation", target=OFF_COMMAND)
    assert classify_state(rec, event(OFF, obs("on", 13)), Runtime(), MATTER, NOW) == Verdict(DEBOUNCE)
    rec.last_command.at = NOW - 15      # the default window is inclusive
    assert classify_state(rec, event(OFF, obs("on", 13)), Runtime(), MATTER, NOW).kind == FAILED_DELIVERY
    rec.last_command.at = NOW - 61      # past even the hue window
    assert classify_state(rec, event(OFF, obs("on", 13)), Runtime(), HUE, NOW) == Verdict(DEBOUNCE)


def test_late_window_never_retries_a_person_turning_the_lamp_up():
    # Motion turns a nightlight on at 13; 20 s later a person turns it up on a
    # dimmer bound to the bridge (no context). Only flips were measured as bridge
    # reversals: a brightness move is the person's and must not be undone.
    nl = Command("on", 13, Color.kelvin(2202))
    rec = record(OFF_COMMAND, layer("nl", 50, nl))
    rec.last_command = LastCommand(NOW - 20, ours=True, source="ours", target=nl, context_id="ctx-nl")
    ev = event(obs("on", 13, kelvin=2202), obs("on", 203, kelvin=2202), ctx="ctx-bare")
    assert classify_state(rec, ev, Runtime(), HUE, NOW) == Verdict(DEBOUNCE)


def test_late_window_ignores_brightness_and_colour_moves():
    rec = record(Command("on", 64))
    rec.last_command = LastCommand(NOW - 30, ours=True, source="ours", target=Command("on", 64))
    assert classify_state(rec, event(obs("on", 64), obs("on", 200)), Runtime(), HUE, NOW).kind == DEBOUNCE
    target = Command("on", 200, Color.xy(0.3, 0.3))
    rec = record(target)
    rec.last_command = LastCommand(NOW - 30, ours=False, source="user", target=target)
    ev = event(obs("on", 200, xy=(0.31, 0.3)), obs("on", 200, xy=(0.5, 0.4)))
    assert classify_state(rec, ev, Runtime(), HUE, NOW).kind == DEBOUNCE


def test_a_flip_during_our_own_render_is_noise_for_its_verification():
    # Hue reports an optimistic off, then corrects it to on 9.7 s later, no context,
    # while our render still waits SLOW_OFF_S: its verification judges and retries.
    rec = record(Command("on", 200), layer("tv", 40, OFF_COMMAND))
    rec.last_command = LastCommand(NOW - 9.7, ours=True, source="ours", target=OFF_COMMAND,
                                   context_id="ctx-r1")
    rt = Runtime(ours={"ctx-r1": OurCommand("ctx-r1", OFF_COMMAND, NOW - 9.7)}, render_alive=True)
    assert classify_state(rec, event(OFF, obs("on", 200)), rt, HUE, NOW) == Verdict(NOISE)
    # Once the render is over, the same flip is the late window's failed delivery.
    verdict = classify_state(rec, event(OFF, obs("on", 200)), Runtime(), HUE, NOW)
    assert (verdict.kind, verdict.ours) == (FAILED_DELIVERY, True)


def test_a_step_towards_our_target_after_a_report_without_brightness_is_noise():
    # One report of our 10 s transition lacked a brightness; the next step is not
    # a reversal, and while our render runs it is noise.
    rec = record(Command("on", 255), layer("nl", 50, Command("on", 13)))
    rec.last_command = LastCommand(NOW - 7, ours=True, source="ours", target=Command("on", 13),
                                   context_id="ctx-ours")
    ev = event(Observed("on", None, "unknown", at=NOW - 1), obs("on", 200, kelvin=2700))
    assert classify_state(rec, ev, Runtime(render_alive=True), HUE, NOW) == Verdict(NOISE)


def test_reports_while_a_return_settles_are_left_to_decide_return():
    # A lamp came back and re-reports its attributes within RETURN_SETTLE_S.
    # Judged on their own they could take back a layer before decide_return runs.
    rec = record(Command("on", 102, WARM), layer("sig", 70, Command("on", 255, Color.xy(0.6, 0.33))),
                 p_at_drop=obs("on", 102, kelvin=2700))
    ev = event(obs("on", 102, kelvin=2700), obs("on", 110, kelvin=2900), ctx="ctx-bare")
    assert classify_state(rec, ev, Runtime(returning=True), HUE, NOW) == Verdict(NOISE)
    assert classify_state(rec, ev, Runtime(), HUE, NOW) == Verdict(DEBOUNCE)
    # A person's change during the settle is still theirs.
    ev = event(obs("on", 102, kelvin=2700), obs("on", 30, kelvin=2700), ctx="ctx-app", user="user-1")
    assert classify_state(rec, ev, Runtime(returning=True), HUE, NOW) == Verdict(EXTERNAL, "user")


def test_transition_steps_towards_the_target_are_not_late():
    # Our 10 s transition outlives HA's 5 s context reuse: later steps have no context.
    rec = record(Command("on", 200), layer("tv", 40, Command(None, 64), "adjust"))
    rec.last_command = LastCommand(NOW - 7, ours=True, source="ours", target=Command("on", 64))
    ev = event(obs("on", 120), obs("on", 90))
    assert classify_state(rec, ev, Runtime(render_alive=True), HUE, NOW) == Verdict(NOISE)
    # A dimming step on the way to off is a step towards it too.
    rec.last_command.target = OFF_COMMAND
    assert classify_state(rec, ev, Runtime(render_alive=True), MATTER, NOW) == Verdict(NOISE)


def test_a_toggle_leaves_nothing_to_judge_a_reversal_against():
    rec = record(OFF_COMMAND)
    rec.last_command = LastCommand(NOW - 5, ours=False, source="user", target=None)
    assert classify_state(rec, event(OFF, ON_200), Runtime(), HUE, NOW) == Verdict(DEBOUNCE)


def test_noise_while_our_render_is_alive():
    ev = event(obs("on", 180), ON_200)
    assert classify_state(record(), ev, Runtime(render_alive=True), HUE, NOW) == Verdict(NOISE)


def test_follow_up_absorbs_an_attribute_tail_of_an_external_change():
    rec = record(Command("on", 100))
    rec.last_external = External(NOW - 4, "user", "take_back")
    ev = event(obs("on", 120), obs("on", 100))
    assert classify_state(rec, ev, Runtime(), MATTER, NOW) == Verdict(FOLLOW_UP, "user")


def test_follow_up_never_absorbs_an_on_off_flip():
    rec = record(OFF_COMMAND)
    rec.last_external = External(NOW - 4, "user", "take_back")
    assert classify_state(rec, event(OFF, ON_200), Runtime(), MATTER, NOW) == Verdict(DEBOUNCE)


def test_follow_up_window_is_10_s():
    rec = record(Command("on", 100))
    rec.last_external = External(NOW - 10.5, "user", "take_back")
    assert classify_state(rec, event(obs("on", 120), obs("on", 100)), Runtime(), MATTER, NOW) == Verdict(
        DEBOUNCE
    )
    rec.last_external = External(NOW - 10, "user", "take_back")      # inclusive
    assert classify_state(rec, event(obs("on", 120), obs("on", 100)), Runtime(), MATTER, NOW) == Verdict(
        FOLLOW_UP, "user"
    )


def test_debounce_otherwise():
    assert classify_state(record(Command("on", 200)), event(ON_200, OFF), Runtime(), HUE, NOW) == Verdict(
        DEBOUNCE
    )


def test_classify_state_does_not_mutate_the_record():
    rec = record(Command("on", 200), layer("tv", 40, OFF_COMMAND), last_layers_change=NOW - 5)
    rec.last_command = LastCommand(NOW - 35, ours=True, source="ours", target=Command("on", 200))
    before = rec.to_json()
    classify_state(rec, event(ON_200, OFF), Runtime(), HUE, NOW)
    assert rec.to_json() == before


# --------------------------------------------------------------------------- #
# 6.2 settle_debounce
# --------------------------------------------------------------------------- #


def test_a_7_ms_resync_blip_that_came_back_is_a_rereport():
    # Incident: a bridge resync reports on -> off -> on within 7 ms, no context.
    rec = record(Command("on", 200), layer("nl", 50, Command("on", 13)))
    first = classify_state(rec, event(obs("on", 13), OFF, at=NOW), Runtime(), HUE, NOW)
    second = classify_state(rec, event(OFF, obs("on", 13), at=NOW + 0.007), Runtime(), HUE, NOW + 0.007)
    assert (first.kind, second.kind) == (DEBOUNCE, DEBOUNCE)
    rec.observed_prev = obs("on", 13, at=NOW - 60)       # stored when the debounce began
    assert settle_debounce(rec, obs("on", 13, at=NOW + 3), HUE) == Verdict(REREPORT)


def test_a_blip_that_came_back_is_a_rereport_even_off_the_effective_command():
    # Only the blip rule can answer here: the lamp is not at its effective command.
    rec = record(Command("on", 200))
    rec.observed_prev = obs("on", 13, at=NOW - 60)
    assert settle_debounce(rec, obs("on", 13, at=NOW + 3), HUE) == Verdict(REREPORT)


def test_settle_a_colour_the_lamp_was_not_asked_for_is_external():
    # The brightness is right but a person changed the colour: colour_off is not
    # "shows its effective command".
    rec = record(Command("on", 200, WARM))
    rec.observed_prev = Observed("on", 200, "color_temp", xy=(0.525, 0.388), kelvin=2700)
    # Only the colour moved: the take-back takes the colour, not the brightness.
    assert settle_debounce(rec, obs("on", 200, xy=(0.2, 0.2)), HUE, NOW) == Verdict(
        EXTERNAL, "device", groups=frozenset({"state", "color"})
    )


def test_settle_a_colour_only_lamp_at_its_emulated_kelvin_is_a_rereport():
    # HA emulates 2700 K on an xy-only lamp; the lamp reports that xy. It shows its
    # effective command, so taking it back as the device's change would drop our layer.
    xy_only = Caps(frozenset({"xy"}), transition=True, platform="matter")
    rec = record(OFF_COMMAND, layer("nl", 50, Command("on", 120, WARM)))
    rec.observed_prev = obs("on", 60, xy=(0.3, 0.3))
    settled = obs("on", 120, xy=(0.525, 0.388))
    assert settle_debounce(rec, settled, xy_only, NOW) == Verdict(REREPORT)


def test_a_real_change_that_held_is_external_from_the_device():
    rec = record(Command("on", 200), layer("nl", 50, Command("on", 13)))
    rec.observed_prev = obs("on", 13)
    assert settle_debounce(rec, OFF, HUE, NOW + 3) == Verdict(EXTERNAL, "device")     # a flip: all
    assert settle_debounce(rec, obs("on", 13 + 20), HUE, NOW + 3) == Verdict(
        EXTERNAL, "device", groups=frozenset({"state", "brightness"})
    )


def test_a_state_matching_the_effective_projection_is_a_rereport():
    rec = record(Command("on", 200), layer("tv", 40, Command(None, 64), "adjust"))
    rec.observed_prev = ON_200                       # before a late step of our own dimming
    assert settle_debounce(rec, obs("on", 66), HUE, NOW) == Verdict(REREPORT)


def test_settle_uses_the_layers_live_at_now():
    rec = record(Command("on", 200), layer("tv", 40, Command(None, 64), "adjust", expires_at=NOW))
    rec.observed_prev = OFF
    assert settle_debounce(rec, obs("on", 64), HUE, NOW - 1) == Verdict(REREPORT)
    assert settle_debounce(rec, obs("on", 64), HUE, NOW) == Verdict(EXTERNAL, "device")
    assert settle_debounce(rec, obs("on", 64, at=NOW), HUE) == Verdict(EXTERNAL, "device")


def test_settle_without_prev_or_effective_is_external():
    assert settle_debounce(record(), ON_200, HUE, NOW) == Verdict(EXTERNAL, "device")


def test_settle_on_a_lamp_that_went_away_again_is_ignored():
    rec = record(Command("on", 200))
    rec.observed_prev = ON_200
    assert settle_debounce(rec, AWAY, HUE, NOW) == Verdict(IGNORE)
    assert settle_debounce(rec, None, HUE, NOW) == Verdict(IGNORE)


# --------------------------------------------------------------------------- #
# 6.3 classify_call
# --------------------------------------------------------------------------- #


def test_turn_off_on_an_unavailable_lamp_is_an_intent():
    # Incident: "Off" pressed while one lamp of the room was unavailable. HA drops it
    # from the call silently; without the intent a later clear relights the room.
    rec = record(Command("on", 64), layer("tv", 40, Command(None, 64), "adjust"),
                 observed=AWAY, available=False, last_layers_change=NOW - 600)
    call = call_info("ctx-off", service="turn_off", user_id="user-1")
    verdict = classify_call(rec, call, HUE, NOW)
    assert verdict == Verdict(EXTERNAL_INTENT, "user", call, frozenset({"state"}), command=OFF_COMMAND)


def test_turn_on_on_an_unavailable_lamp_is_an_intent_with_the_call_groups():
    rec = record(OFF_COMMAND, observed=AWAY, available=False)
    call = call_info(command=Command("on", 50))
    verdict = classify_call(rec, call, HUE, NOW)
    assert (verdict.kind, verdict.command, verdict.groups) == (
        EXTERNAL_INTENT, Command("on", 50), frozenset({"state", "brightness"}))


def test_unknown_intents_on_an_unavailable_lamp_are_lost():
    rec = record(OFF_COMMAND, observed=AWAY, available=False)
    assert classify_call(rec, call_info(service="toggle"), HUE, NOW) is None
    assert classify_call(rec, call_info(groups=frozenset({"brightness"})), HUE, NOW) is None  # brightness_step


def test_turn_off_on_a_lamp_already_off_is_an_intent():
    # Incident: the rocker's Off while the tv layer already holds the lamp off. No
    # state_changed follows, and without the intent the film's end relights it.
    rec = record(Command("on", 102), layer("tv", 40, OFF_COMMAND), observed=OFF)
    call = call_info("ctx-rocker", service="turn_off")
    verdict = classify_call(rec, call, HUE, NOW)
    assert verdict == Verdict(EXTERNAL_INTENT, "automation", call, frozenset({"state"}), command=OFF_COMMAND)
    # A turn_on to brightness 0 is an off intent too.
    zero = call_info("ctx-zero", command=OFF_COMMAND, groups=frozenset({"state"}))
    assert classify_call(rec, zero, HUE, NOW).command == OFF_COMMAND


def test_turn_off_on_a_lamp_that_is_on_goes_to_the_state_path():
    rec = record(Command("on", 200), observed=ON_200)
    assert classify_call(rec, call_info(service="turn_off"), HUE, NOW) is None


def test_turn_on_with_attributes_already_shown_is_an_intent():
    rec = record(Command("on", 200), layer("nl", 50, Command("on", 13, Color.kelvin(2400))),
                 observed=obs("on", 14, kelvin=2200))
    # The lamp floors at 2200 K, so a 2000 K request shows as 2200 K: no state change.
    command = Command("on", 13, Color.kelvin(2000))
    call = call_info("ctx-dim", command=command)
    verdict = classify_call(rec, call, HUE, NOW)
    assert verdict == Verdict(EXTERNAL_INTENT, "automation", call, frozenset({"state", "brightness", "color"}),
                              command=command)
    # xy within tolerance, brightness only.
    rec.observed = obs("on", 255, xy=(0.61, 0.33))
    assert classify_call(rec, call_info(command=Command("on", None, Color.xy(0.6, 0.32))), HUE, NOW).kind == (
        EXTERNAL_INTENT)
    assert classify_call(rec, call_info(command=Command("on", 252)), HUE, NOW).kind == EXTERNAL_INTENT


def test_turn_on_with_attributes_not_shown_goes_to_the_state_path():
    rec = record(Command("on", 200), observed=ON_200)
    assert classify_call(rec, call_info(command=Command("on", 50)), HUE, NOW) is None
    rec.observed = obs("on", 200, kelvin=4000)
    assert classify_call(rec, call_info(command=Command("on", 200, WARM)), HUE, NOW) is None


def test_other_calls_go_to_the_state_path():
    rec = record(Command("on", 200), observed=ON_200)
    assert classify_call(rec, call_info(command=Command("on")), HUE, NOW) is None       # bare turn_on
    assert classify_call(rec, call_info(service="toggle"), HUE, NOW) is None
    rec.observed = OFF
    assert classify_call(rec, call_info(command=Command("on", 200)), HUE, NOW) is None  # turns it on
    assert classify_call(record(Command("on", 200)), call_info(command=Command("on", 200)), HUE, NOW) is None
    other = call_info(service="turn_off", lamps=frozenset({OTHER}))
    assert classify_call(record(OFF_COMMAND, observed=OFF), other, HUE, NOW) is None


def test_intent_from_a_replayed_press_is_a_replay():
    rec = record(Command("on", 102), layer("tv", 40, OFF_COMMAND), observed=OFF,
                 last_layers_change=NOW - 10)
    call = call_info("ctx-loop", service="turn_off", first_seen=NOW - 31)
    assert classify_call(rec, call, HUE, NOW).replay is True


def test_brightness_only_lamp_ignores_a_colour_it_cannot_show():
    rec = record(Command("on", 100), observed=obs("on", 100))
    call = call_info(command=Command("on", 100, Color.xy(0.6, 0.3)))
    assert classify_call(rec, call, DIMMER, NOW).kind == EXTERNAL_INTENT


def test_an_intent_needs_an_attribute_the_lamp_takes_and_can_be_compared_on():
    # Otherwise the projection drops everything and "already shows" is vacuous, and
    # take_back drops the lamp's layers for a call that showed nothing.
    ct_only = Caps(frozenset({"color_temp"}), 2202, 6500, platform="hue")
    switch = Caps(frozenset({"onoff"}), platform="zha")
    nl = layer("nl", 50, Command("on", 13, Color.kelvin(2202)))
    rec = record(Command("on", 200, WARM), nl, observed=obs("on", 13, kelvin=2202))
    red = call_info(command=Command("on", None, Color.xy(0.68, 0.31)))
    assert classify_call(rec, red, ct_only, NOW) is None
    rec.observed = obs("on", 200, xy=(0.64, 0.33))
    blue_hs = call_info(command=Command("on", None, Color("hs_color", (240.0, 100.0))))
    blue_rgb = call_info(command=Command("on", None, Color("rgb_color", (0.0, 0.0, 255.0))))
    assert classify_call(rec, blue_hs, HUE, NOW) is None
    assert classify_call(rec, blue_rgb, HUE, NOW) is None
    rec.observed = obs("on", 90)
    assert classify_call(rec, call_info(command=Command("on", None, Color.kelvin(3000))), DIMMER, NOW) is None
    rec.observed = Observed("on", None, "onoff", at=NOW)
    assert classify_call(rec, call_info(command=Command("on", 255)), switch, NOW) is None


def test_turn_on_intent_on_an_unavailable_lamp_takes_the_state():
    rec = record(OFF_COMMAND, observed=AWAY, available=False)
    call = call_info(command=Command("on", 50), groups=frozenset({"brightness"}))
    assert classify_call(rec, call, HUE, NOW).groups == frozenset({"state", "brightness"})


def test_kelvin_intent_on_a_colour_only_lamp_showing_it_is_an_intent():
    xy_only = Caps(frozenset({"xy"}), transition=True, platform="matter")
    rec = record(Command("on", 200), layer("nl", 50, Command("on", 13)),
                 observed=obs("on", 102, xy=(0.525, 0.388)))
    call = call_info(command=Command("on", 102, WARM))
    assert classify_call(rec, call, xy_only, NOW).kind == EXTERNAL_INTENT


# --------------------------------------------------------------------------- #
# 6.4 decide_return
# --------------------------------------------------------------------------- #


def test_return_with_no_effective_command_records():
    rec = record(None, layer("dim", 40, Command(None, 10), "adjust"), p_at_drop=ON_200)
    assert decide_return(rec, obs("on", 254), HUE, NOW) == ReturnDecision(RECORD)


def test_return_already_matching_does_nothing_and_clears_owed():
    rec = record(Command("on", 200), p_at_drop=OFF, owed=Owed(NOW - 30, Command("on", 200), turns_on=True))
    decision = decide_return(rec, obs("on", 202), HUE, NOW)
    assert decision == ReturnDecision(NOTHING)
    assert decision.clears_owed


def test_return_untouched_with_a_named_layer_active_sends():
    rec = record(OFF_COMMAND, layer("sig", 70, Command("on", 255, Color.xy(0.6, 0.3))), p_at_drop=OFF)
    decision = decide_return(rec, OFF, HUE, NOW)
    assert decision == ReturnDecision(SEND)
    assert not decision.clears_owed


def test_return_untouched_with_an_owed_off_sends():
    rec = record(OFF_COMMAND, p_at_drop=ON_200,
                 owed=Owed(NOW - 3 * OWED_ON_MAX_AGE_S, OFF_COMMAND, turns_on=False))
    assert decide_return(rec, obs("on", 199), HUE, NOW) == ReturnDecision(SEND)


def test_return_untouched_with_an_owed_on_sends_only_while_fresh():
    # Incident: an owed release must not light a room in the night, hours after the fact.
    rec = record(Command("on", 102), p_at_drop=OFF,
                 owed=Owed(NOW - OWED_ON_MAX_AGE_S - 1, Command("on", 102), turns_on=True))
    decision = decide_return(rec, OFF, HUE, NOW)
    assert decision == ReturnDecision(RECORD)
    assert decision.clears_owed
    rec.owed.since = NOW - 60
    assert decide_return(rec, OFF, HUE, NOW) == ReturnDecision(SEND)
    missed = Owed(NOW - 60, Command("on", 102), turns_on=True, missed=True)
    rec.owed = missed
    assert decide_return(rec, OFF, HUE, NOW) == ReturnDecision(SEND)


def test_return_untouched_with_nothing_owed_and_base_only_records():
    rec = record(Command("on", 102), p_at_drop=OFF)
    assert decide_return(rec, OFF, HUE, NOW) == ReturnDecision(RECORD)


def test_return_changed_while_away_is_external_from_the_device():
    # A power cycle at a wall switch: the lamp comes back at its power-on default.
    rec = record(OFF_COMMAND, layer("nl", 50, Command("on", 13)), p_at_drop=OFF,
                 owed=Owed(NOW - 30, Command("on", 13), turns_on=True))
    decision = decide_return(rec, obs("on", 254), HUE, NOW)
    assert decision == ReturnDecision(EXTERNAL, "device")
    assert decision.clears_owed


def test_return_with_p_unknown_and_nothing_to_deliver_records():
    rec = record(Command("on", 102), p_at_drop=None)
    assert decide_return(rec, ON_200, HUE, NOW) == ReturnDecision(RECORD)
    rec.p_at_drop = AWAY
    assert decide_return(rec, ON_200, HUE, NOW) == ReturnDecision(RECORD)


def test_return_with_p_unknown_reasserts_a_holding_layer():
    # A lamp away since before startup comes back on while a hold keeps it off:
    # it never got the hold's command. Nobody can tell it was touched, so the
    # holding layer is sent (EXTERNAL needs evidence of a change).
    rec = record(Command("on", 102), layer("tv", 40, OFF_COMMAND), p_at_drop=None)
    assert decide_return(rec, ON_200, HUE, NOW) == ReturnDecision(SEND)


def test_return_with_p_unknown_delivers_a_missed_off():
    # Off pressed while the lamp was away (it was away since startup, so P is
    # unknown). An off never lights a room: it is delivered, not recorded away.
    rec = record(OFF_COMMAND, p_at_drop=None, owed=Owed(NOW - 600, OFF_COMMAND, missed=True))
    assert decide_return(rec, obs("on", 102), HUE, NOW) == ReturnDecision(SEND)


def test_return_with_p_unknown_only_sends_an_owed_render_that_cannot_light():
    # A release of an expired signal to a dimmer base: it dims, so it is sent even
    # though nobody can tell whether the lamp was touched.
    rec = record(Command("on", 102, WARM), p_at_drop=None,
                 owed=Owed(NOW - 30, Command("on", 102, WARM), turns_on=True))
    assert decide_return(rec, obs("on", 255, xy=(0.6, 0.33)), HUE, NOW) == ReturnDecision(SEND)
    # One that would light a dark lamp is not.
    assert decide_return(rec, OFF, HUE, NOW) == ReturnDecision(RECORD)


def test_return_an_owed_release_that_only_dims_is_sent_however_old():
    # The age limit is for owed renders that would light a room hours later. A
    # restart that outlasted it during a signal's release must not record the
    # signal as the base.
    rec = record(Command("on", 102, WARM), p_at_drop=obs("on", 255, xy=(0.6, 0.33)),
                 owed=Owed(NOW - 3 * OWED_ON_MAX_AGE_S, Command("on", 102, WARM), turns_on=True))
    assert decide_return(rec, obs("on", 255, xy=(0.6, 0.33)), HUE, NOW) == ReturnDecision(SEND)


def test_return_an_owed_off_whose_target_is_now_on_does_not_light_hours_later():
    # The owed render was an off; the layer was cleared while the lamp was away, so
    # sending now would turn it on. That is the age-limited case.
    rec = record(Command("on", 150), p_at_drop=OFF,
                 owed=Owed(NOW - 2 * OWED_ON_MAX_AGE_S, OFF_COMMAND, turns_on=False))
    assert decide_return(rec, OFF, HUE, NOW) == ReturnDecision(RECORD)
    rec.owed.since = NOW - 60
    assert decide_return(rec, OFF, HUE, NOW) == ReturnDecision(SEND)


def test_return_owed_on_age_limit_is_inclusive():
    rec = record(Command("on", 102), p_at_drop=OFF,
                 owed=Owed(NOW - OWED_ON_MAX_AGE_S, Command("on", 102), turns_on=True))
    assert decide_return(rec, OFF, HUE, NOW) == ReturnDecision(SEND)


def test_return_an_adjust_layer_never_turns_a_lamp_on():
    # An owed ON under an adjust layer, 6 h old: the adjust layer is not a holder,
    # so the owed-ON age limit applies and the lamp stays dark.
    rec = record(Command("on", 102, WARM), layer("dim", 30, Command(None, 60), "adjust"), p_at_drop=OFF,
                 owed=Owed(NOW - 6 * 3600, Command("on", 60, WARM), turns_on=True))
    assert decide_return(rec, OFF, HUE, NOW) == ReturnDecision(RECORD)


def test_return_an_adjust_layer_on_a_lit_untouched_lamp_is_sent():
    # Sending only changes the attributes of a lamp that is on anyway.
    rec = record(Command("on", 150), layer("dim", 30, Command(None, 64), "adjust"), p_at_drop=obs("on", 150))
    assert decide_return(rec, obs("on", 150), HUE, NOW) == ReturnDecision(SEND)


@pytest.mark.parametrize("diverged", ["manual_keep", "unsynced"])
def test_return_untouched_does_not_push_a_lamp_only_sync_may_push(diverged):
    # A person's change kept on show (or a lamp left unsynced) blips unavailable and
    # comes back untouched: only layers.sync pushes it.
    rec = record(Command("on", 150, WARM), layer("tv", 40, OFF_COMMAND), p_at_drop=obs("on", 150, kelvin=2700),
                 diverged=diverged)
    assert decide_return(rec, obs("on", 150, kelvin=2700), MATTER, NOW) == ReturnDecision(NOTHING)
    # A render owed since then is still delivered; a lamp touched while away is external.
    rec.owed = Owed(NOW - 5, OFF_COMMAND)
    assert decide_return(rec, obs("on", 150, kelvin=2700), MATTER, NOW) == ReturnDecision(SEND)
    rec.owed = None
    assert decide_return(rec, obs("on", 30, kelvin=2700), MATTER, NOW) == ReturnDecision(EXTERNAL, "device")


def test_return_of_a_colour_only_lamp_already_showing_its_kelvin_does_nothing():
    xy_only = Caps(frozenset({"xy"}), transition=True, platform="matter")
    shows = obs("on", 120, xy=(0.525, 0.388))
    rec = record(OFF_COMMAND, layer("nl", 50, Command("on", 120, WARM)), p_at_drop=shows)
    assert decide_return(rec, shows, xy_only, NOW) == ReturnDecision(NOTHING)
    rec.p_at_drop = OFF
    assert decide_return(rec, shows, xy_only, NOW) == ReturnDecision(NOTHING)


def test_return_that_is_not_back_waits_and_keeps_owed():
    rec = record(Command("on", 102), p_at_drop=OFF, owed=Owed(NOW - 30, OFF_COMMAND))
    for shown in (AWAY, obs("unknown"), None):
        decision = decide_return(rec, shown, HUE, NOW)
        assert decision == ReturnDecision(WAIT)
        assert not decision.clears_owed


def test_matter_off_unavailable_off_does_nothing():
    # Incident: a Matter lamp drops off -> unavailable -> off. Nothing to do.
    rec = record(OFF_COMMAND)
    assert classify_state(rec, event(OFF, AWAY), Runtime(), MATTER, NOW).kind == TRANSPORT_DOWN
    rec.p_at_drop = OFF
    assert classify_state(rec, event(AWAY, OFF), Runtime(), MATTER, NOW).kind == TRANSPORT_UP
    assert decide_return(rec, OFF, MATTER, NOW + 5) == ReturnDecision(NOTHING)


def test_decide_return_does_not_mutate_the_record():
    rec = record(Command("on", 102), layer("tv", 40, OFF_COMMAND), p_at_drop=ON_200,
                 owed=Owed(NOW - 30, OFF_COMMAND))
    before = rec.to_json()
    decide_return(rec, ON_200, HUE, NOW)
    settle_debounce(rec, ON_200, HUE, NOW)
    classify_call(rec, call_info(service="turn_off"), HUE, NOW)
    assert rec.to_json() == before


# --------------------------------------------------------------------------- #
# Review fixes: late window, follow-ups, room calls, debounce groups
# --------------------------------------------------------------------------- #


def test_late_window_counts_only_a_flip_back_to_where_the_lamp_was():
    # Our dim of a lamp that was already on (on -> on at 64). An off 20 s later is
    # not the bridge undoing it (that would leave the lamp on): it is a person.
    rec = record(Command("on", 102), layer("tv", 40, Command(None, 64), "adjust"))
    rec.last_command = LastCommand(NOW - 20, ours=True, source="ours", target=Command("on", 64),
                                   from_state="on")
    assert classify_state(rec, event(obs("on", 64), OFF), Runtime(), HUE, NOW) == Verdict(DEBOUNCE)
    # The same command sent to a lamp that was off, reverted to off: the measured reversal.
    rec.last_command.from_state = "off"
    verdict = classify_state(rec, event(obs("on", 64), OFF), Runtime(), HUE, NOW)
    assert (verdict.kind, verdict.ours) == (FAILED_DELIVERY, True)
    # Unknown from_state (stored before the field existed): judged as before.
    rec.last_command.from_state = None
    assert classify_state(rec, event(obs("on", 64), OFF), Runtime(), HUE, NOW).kind == FAILED_DELIVERY


def test_a_foreign_off_that_turned_nothing_off_cannot_be_reversed():
    # A house-wide Off reached a lamp a layer already held off, then a person turns it
    # on 20 s later at a Hue dimmer: not a failed delivery of the Off.
    rec = record(OFF_COMMAND, layer("tv", 40, OFF_COMMAND))
    rec.last_command = LastCommand(NOW - 20, ours=False, source="automation", target=OFF_COMMAND,
                                   from_state="off")
    assert classify_state(rec, event(OFF, obs("on", 150)), Runtime(), HUE, NOW) == Verdict(DEBOUNCE)


def test_a_reverted_foreign_on_is_a_change_not_a_delivery_to_repair():
    # A person turns the lamp on from the app; 2 s later it goes off with no context
    # (the wall switch). Marking it a failed delivery would make the next layers.*
    # call on the lamp re-send the ON: a dark room lit. It is judged as a change.
    rec = record(Command("on", 200))
    rec.last_command = LastCommand(NOW - 2, ours=False, source="user", target=Command("on", 200),
                                   from_state="off")
    assert classify_state(rec, event(ON_200, OFF), Runtime(), HUE, NOW) == Verdict(DEBOUNCE)
    # A reverted foreign OFF is still a delivery to repair: re-sending it lights nothing.
    rec.last_command = LastCommand(NOW - 9.7, ours=False, source="automation", target=OFF_COMMAND,
                                   from_state="on")
    verdict = classify_state(rec, event(OFF, obs("on", 13)), Runtime(), HUE, NOW)
    assert (verdict.kind, verdict.ours) == (FAILED_DELIVERY, False)


def test_no_follow_up_after_a_layer_change_or_a_render_of_ours():
    # A person set 200 from the app; the owner then set a layer (rendered at 50). The
    # lamp's no-context report at 52 is our render landing, not a tail of the person's.
    rec = record(Command("on", 200), layer("sig", 70, Command("on", 50)))
    rec.last_external = External(NOW - 6, "user", "take_back")
    ev = event(obs("on", 50), obs("on", 52))
    rec.last_layers_change = NOW - 4
    assert classify_state(rec, ev, Runtime(), MATTER, NOW) == Verdict(DEBOUNCE)
    rec.last_layers_change = NOW - 8
    rec.last_command = LastCommand(NOW - 3, ours=True, source="ours", target=Command("on", 50))
    assert classify_state(rec, ev, Runtime(), MATTER, NOW) == Verdict(DEBOUNCE)
    # Nothing of ours since the person's change: a tail.
    rec.last_command = LastCommand(NOW - 7, ours=True, source="ours", target=Command("on", 50))
    assert classify_state(rec, ev, Runtime(), MATTER, NOW) == Verdict(FOLLOW_UP, "user")


def test_a_room_call_is_not_blamed_for_a_report_that_contradicts_it():
    # A room Off, then our nightlight turned the member on: a no-context "on" report
    # is not the room's Off.
    room = call_info("ctx-room", service="turn_off", lamps=frozenset({LAMP}),
                     via_room=frozenset({LAMP}))
    rec = record(OFF_COMMAND, layer("nl", 50, Command("on", 13)))
    rt = Runtime(room_call=room)
    assert classify_state(rec, event(OFF, obs("on", 14)), rt, HUE, NOW) == Verdict(DEBOUNCE)
    assert classify_state(rec, event(obs("on", 14), OFF), rt, HUE, NOW).kind == EXTERNAL
    # A toggle has no intent to contradict.
    toggle = call_info("ctx-t", service="toggle", groups=frozenset(), lamps=frozenset({LAMP}),
                       via_room=frozenset({LAMP}))
    assert classify_state(rec, event(OFF, obs("on", 14)), Runtime(room_call=toggle), HUE, NOW).kind == EXTERNAL


def test_a_dimmer_on_a_signal_colour_takes_only_the_brightness():
    # The trash signal (red) is showing; a person dims it at a Hue dimmer. The base
    # must learn the brightness, not the signal colour.
    red = Color.xy(0.6, 0.35)
    rec = record(Command("on", 102, WARM), layer("trash", 70, Command("on", 255, red)))
    rec.observed_prev = obs("on", 255, xy=(0.6, 0.35))
    verdict = settle_debounce(rec, obs("on", 128, xy=(0.6, 0.35)), HUE, NOW)
    assert verdict == Verdict(EXTERNAL, "device", groups=frozenset({"state", "brightness"}))


def test_changed_groups():
    from layers_logic.classify import changed_groups

    warm = obs("on", 100, kelvin=2700)
    assert changed_groups(warm, obs("on", 100, kelvin=2710)) is None          # nothing moved
    assert changed_groups(warm, obs("on", 180, kelvin=2700)) == frozenset({"state", "brightness"})
    assert changed_groups(warm, obs("on", 100, kelvin=4000)) == frozenset({"state", "color"})
    assert changed_groups(warm, obs("on", 180, xy=(0.3, 0.3))) == frozenset(
        {"state", "brightness", "color"})
    assert changed_groups(warm, OFF) is None                                    # a flip
    assert changed_groups(OFF, warm) is None
    assert changed_groups(None, warm) is None
    assert changed_groups(AWAY, warm) is None


# --------------------------------------------------------------------------- #
# 6.1 A person acting while our render runs
# --------------------------------------------------------------------------- #


def _rendering(target: Command, *, at: float = NOW - 3) -> tuple[Record, Runtime]:
    rec = record(Command("on", 200), layer("tv", 40, target, "set" if target.state else "adjust"))
    rec.last_command = LastCommand(at, ours=True, source="ours", target=target, context_id="ctx-r")
    return rec, Runtime(render_alive=True, ours={"ctx-r": OurCommand("ctx-r", target, at)})


def test_a_dimmer_moving_away_from_our_target_during_our_render_is_a_person():
    # Our dim to 64 is in flight; a report with no context (a Hue dimmer) has the lamp
    # jump to 180. Verification would re-send 64 over them six times: it is the
    # person's change, and the engine cancels the render.
    rec, rt = _rendering(Command("on", 64), at=NOW - 5)     # past RAMP_GRACE_S
    ev = event(obs("on", 70, kelvin=2700), obs("on", 180, kelvin=2700))
    assert classify_state(rec, ev, rt, HUE, NOW) == Verdict(EXTERNAL, "device")


def test_a_move_away_inside_the_ramp_grace_is_the_lamp_ramping():
    # Incident 2026-09-16: an IKEA Matter globe answered our nightlight (26) with
    # "on" at its old level, then ramped down, all inside a second and with no
    # context. The second report was "further" than the first and the globe was
    # taken back: it sat at the nightlight level all day. Inside RAMP_GRACE_S of our
    # command a move away is the lamp, not a person.
    rec, rt = _rendering(Command("on", 26), at=NOW - 0.5)
    ev = event(obs("on", 26, kelvin=2202), obs("on", 255, kelvin=2202))
    assert classify_state(rec, ev, rt, MATTER, NOW) == Verdict(NOISE)
    # The same move 3 s after the command is a person at a dimmer, as before.
    rec, rt = _rendering(Command("on", 26), at=NOW - 3.5)
    assert classify_state(rec, ev, rt, MATTER, NOW) == Verdict(EXTERNAL, "device")


def test_a_step_towards_our_target_during_our_render_is_still_noise():
    rec, rt = _rendering(Command("on", 64))
    ev = event(obs("on", 200, kelvin=2700), obs("on", 120, kelvin=2700))     # a transition step
    assert classify_state(rec, ev, rt, HUE, NOW) == Verdict(NOISE)


def test_a_flip_during_our_render_is_still_noise():
    # A bridge's optimistic off corrected later, or a person: not told apart here.
    rec, rt = _rendering(OFF_COMMAND)
    assert classify_state(rec, event(OFF, obs("on", 200)), rt, HUE, NOW) == Verdict(NOISE)


def test_a_move_away_that_carries_our_context_is_the_lamp_answering_our_call():
    # Inside HA's 5 s context reuse a lamp that clamps our 64 to 200 reports with our
    # context: verification judges it (and fails loudly), it is not a take-back.
    rec, rt = _rendering(Command("on", 64))
    ev = event(obs("on", 100, kelvin=2700), obs("on", 200, kelvin=2700), ctx="ctx-r")
    assert classify_state(rec, ev, rt, HUE, NOW) == Verdict(NOISE)
