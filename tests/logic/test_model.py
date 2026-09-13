"""Round-trip and merge rules of the shared model."""

from layers_logic.model import (
    OFF_COMMAND,
    Call,
    Color,
    Command,
    Layer,
    Observed,
    Owed,
    Record,
    Tombstone,
    merge_command,
)


def test_command_round_trip():
    for cmd in (
        Command("on", 102, Color.kelvin(2700)),
        Command("on", 255, Color.xy(0.61, 0.36)),
        Command("off"),
        Command(None, 64),
        Command("on", None, Color("rgb_color", (200, 40, 10))),
    ):
        assert Command.from_json(cmd.to_json()) == cmd


def test_merge_keeps_what_is_not_replaced():
    below = Command("on", 102, Color.kelvin(2700))
    assert merge_command(below, Command(None, 64)) == Command("on", 64, Color.kelvin(2700))
    assert merge_command(below, Command("off")) == OFF_COMMAND
    assert merge_command(OFF_COMMAND, Command("on")) == Command("on")
    assert merge_command(None, Command(None, 64)) == Command(None, 64)


def test_merge_only_listed_groups():
    below = Command("on", 102, Color.kelvin(2700))
    shown = Command("on", 30, Color.xy(0.6, 0.3))
    assert merge_command(below, shown, frozenset({"brightness"})) == Command("on", 30, Color.kelvin(2700))


def test_record_round_trip():
    rec = Record(
        "light.lamp_a",
        base=Command("on", 102, Color.kelvin(2700)),
        observed=Observed("on", 102, "color_temp", kelvin=2702, at=5.0),
        owed=Owed(1.0, Command("off"), turns_on=False),
        diverged="delivery",
    )
    rec.layers["tv"] = Layer("tv", 40, "set", Command("off"), Command("off"), 3, 2.0, 99.0, "owner")
    rec.tombstones["nl"] = Tombstone("nl", 4.0, None, True, "user")
    back = Record.from_json("light.lamp_a", rec.to_json())
    assert back.to_json() == rec.to_json()
    assert back.worth_persisting()


def test_layer_requested_mode_round_trips_and_defaults_to_none():
    edited = Layer("dim", 40, "set", Command(None, 64), Command("off"), 3, 2.0, requested_mode="adjust")
    assert Layer.from_json(edited.to_json()) == edited
    stored_before_the_field = {k: v for k, v in edited.to_json().items() if k != "requested_mode"}
    assert Layer.from_json(stored_before_the_field).requested_mode is None


def test_call_is_hashable_and_round_trips_lists():
    call = Call.make("turn_on", {"xy_color": [0.1, 0.2], "brightness": 5})
    assert hash(call) == hash(Call.make("turn_on", {"brightness": 5, "xy_color": [0.1, 0.2]}))
    assert call.as_dict() == {"brightness": 5, "xy_color": [0.1, 0.2]}


def test_last_command_and_external_round_trip_their_new_fields():
    from layers_logic.model import External, LastCommand

    last = LastCommand(1.0, True, "ours", Command("on", 13), "ctx", from_state="off")
    back = LastCommand.from_json(last.to_json())
    assert (back.from_state, back.target) == ("off", Command("on", 13))
    assert back.context_id is None                      # contexts are never persisted
    assert last.flips() and not LastCommand(1.0, False, "user", Command("on"), from_state="on").flips()
    assert not LastCommand(1.0, False, "user", Command("on")).flips()     # unknown from_state
    ext = External(2.0, "user", "take_back", groups=frozenset({"state", "brightness"}))
    assert External.from_json(ext.to_json()) == ext
    old = {k: v for k, v in ext.to_json().items() if k != "groups"}
    assert External.from_json(old).groups is None
    assert LastCommand.from_json({"at": 1.0, "ours": True, "source": "ours"}).from_state is None
