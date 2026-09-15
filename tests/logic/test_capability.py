"""Capabilities, reports, projection and verification (SPEC section 4)."""

from __future__ import annotations

from enum import IntFlag, StrEnum

from layers_logic.capability import (
    caps_from_attrs,
    close,
    compared_groups,
    kelvin_to_xy,
    matches,
    observed_from_state,
    observed_to_command,
    project,
    raises_output,
)
from layers_logic.model import (
    OFF_COMMAND,
    TOL_BRIGHTNESS,
    TOL_KELVIN,
    TOL_XY,
    Call,
    Caps,
    Color,
    Command,
    Observed,
)

# Made-up lamps, shaped like the kinds Home Assistant reports.
FULL = Caps(frozenset({"color_temp", "hs", "xy"}), 2000, 6500, True, "hue")
CT_ONLY = Caps(frozenset({"color_temp"}), 2202, 6500, True, "hue")      # CT only, warm floor 2202 K
COLOUR_ONLY = Caps(frozenset({"xy"}), None, None, False, "matter")
HS_ONLY = Caps(frozenset({"hs"}))
RGBWW = Caps(frozenset({"rgbww"}))
DIMMER = Caps(frozenset({"brightness"}))
SWITCH = Caps(frozenset({"onoff"}))
WHITE = Caps(frozenset({"white", "hs"}))
NO_CAPS = Caps()

RED_XY = Color.xy(0.64, 0.33)
WARM = Color.kelvin(2700)


class ColorMode(StrEnum):
    """Stand-in for Home Assistant's ColorMode (a StrEnum), without importing it."""

    COLOR_TEMP = "color_temp"
    HS = "hs"
    XY = "xy"
    ONOFF = "onoff"


class Feature(IntFlag):
    """Stand-in for LightEntityFeature."""

    EFFECT = 4
    FLASH = 8
    TRANSITION = 32


def on(brightness=None, mode=None, *, xy=None, hs=None, kelvin=None) -> Observed:
    return Observed("on", brightness, mode, xy, hs, kelvin, at=1.0)


def turn_on(**data) -> Call:
    return Call.make("turn_on", data)


# --------------------------------------------------------------------------- #
# caps_from_attrs
# --------------------------------------------------------------------------- #


def test_caps_from_attrs_reads_modes_range_and_transition():
    caps = caps_from_attrs(
        {
            "supported_color_modes": ["color_temp", "hs", "xy"],
            "min_color_temp_kelvin": 2202,
            "max_color_temp_kelvin": 6500,
            "supported_features": 44,       # 32 | 8 | 4
            "friendly_name": "Lamp A",
        },
        "hue",
    )
    assert caps == Caps(frozenset({"color_temp", "hs", "xy"}), 2202, 6500, True, "hue")


def test_caps_transition_is_bit_32_only():
    assert caps_from_attrs({"supported_features": 32}, "x").transition is True
    assert caps_from_attrs({"supported_features": 12}, "x").transition is False
    assert caps_from_attrs({"supported_features": 0}, "x").transition is False
    assert caps_from_attrs({"supported_features": Feature.TRANSITION | Feature.FLASH}, "x").transition is True
    assert caps_from_attrs({"supported_features": Feature.EFFECT}, "x").transition is False


def test_caps_from_enum_modes():
    caps = caps_from_attrs({"supported_color_modes": {ColorMode.COLOR_TEMP, ColorMode.XY}}, "matter")
    assert caps.modes == frozenset({"color_temp", "xy"})
    assert all(type(m) is str for m in caps.modes)


def test_caps_from_missing_attributes():
    assert caps_from_attrs({}, "zha") == Caps(frozenset(), None, None, False, "zha")
    assert caps_from_attrs({"supported_color_modes": None, "supported_features": None}, "") == Caps()
    assert caps_from_attrs({"supported_color_modes": ["onoff"]}, "tasmota").modes == frozenset({"onoff"})


# --------------------------------------------------------------------------- #
# observed_from_state
# --------------------------------------------------------------------------- #


def test_observed_from_state_on_xy_lamp():
    obs = observed_from_state(
        "on",
        {
            "brightness": 128,
            "color_mode": ColorMode.XY,
            "xy_color": (0.64, 0.33),
            "hs_color": (0.0, 100.0),
            "rgb_color": (255, 0, 0),
            "color_temp_kelvin": None,
            "supported_color_modes": ["color_temp", "xy"],
        },
        12.5,
    )
    assert obs == Observed("on", 128, "xy", (0.64, 0.33), (0.0, 100.0), None, 12.5)
    assert type(obs.color_mode) is str


def test_observed_from_state_ct_lamp_with_lists():
    obs = observed_from_state(
        "on",
        {"brightness": 102.0, "color_mode": "color_temp", "color_temp_kelvin": 2702, "xy_color": [0.46, 0.41]},
        3.0,
    )
    assert obs == Observed("on", 102, "color_temp", (0.46, 0.41), None, 2702, 3.0)
    assert isinstance(obs.brightness, int)
    assert isinstance(obs.xy, tuple)


def test_observed_from_state_off_lamp():
    attrs = {"brightness": None, "color_mode": None, "xy_color": None, "hs_color": None, "color_temp_kelvin": None}
    assert observed_from_state("off", attrs, 1.0) == Observed("off", at=1.0)


def test_observed_from_state_unavailable_and_missing_attributes():
    obs = observed_from_state("unavailable", {"friendly_name": "Lamp A", "supported_color_modes": ["xy"]}, 7.0)
    assert obs == Observed("unavailable", at=7.0)
    assert not obs.available
    assert observed_from_state("unknown", {}, 0.0) == Observed("unknown")
    assert observed_from_state("on", {}, 0.0) == Observed("on")


def test_observed_from_state_malformed_attributes_are_none():
    obs = observed_from_state(
        "on", {"brightness": "bright", "xy_color": (0.1,), "hs_color": "red", "color_temp_kelvin": [2700]}, 0.0
    )
    assert obs == Observed("on")


# --------------------------------------------------------------------------- #
# observed_to_command
# --------------------------------------------------------------------------- #


def test_observed_to_command_unavailable_unknown_or_missing_is_none():
    assert observed_to_command(Observed("unavailable"), FULL) is None
    assert observed_to_command(Observed("unknown"), FULL) is None
    assert observed_to_command(None, FULL) is None


def test_observed_to_command_off():
    assert observed_to_command(Observed("off"), FULL) == OFF_COMMAND


def test_observed_to_command_color_temp_records_kelvin():
    obs = on(102, "color_temp", xy=(0.46, 0.41), hs=(28.0, 65.0), kelvin=2702)
    assert observed_to_command(obs, FULL) == Command("on", 102, Color.kelvin(2702))


def test_observed_to_command_xy_lamp_records_xy_exactly():
    obs = on(255, "xy", xy=(0.64, 0.33), hs=(0.0, 100.0))
    cmd = observed_to_command(obs, FULL)
    assert cmd == Command("on", 255, Color.xy(0.64, 0.33))
    assert cmd.color.value == (0.64, 0.33)


def test_observed_to_command_every_colour_mode_records_xy():
    for mode in ("xy", "hs", "rgb", "rgbw", "rgbww"):
        obs = on(40, mode, xy=(0.3, 0.3), hs=(200.0, 50.0))
        assert observed_to_command(obs, FULL) == Command("on", 40, Color.xy(0.3, 0.3)), mode


def test_observed_to_command_falls_back_to_hs_without_xy():
    obs = on(40, "hs", hs=(200.0, 50.0))
    assert observed_to_command(obs, HS_ONLY) == Command("on", 40, Color("hs_color", (200.0, 50.0)))


def test_observed_to_command_brightness_onoff_white_record_no_colour():
    for mode in ("brightness", "onoff", "white"):
        obs = on(None if mode == "onoff" else 90, mode, xy=(0.3, 0.3), hs=(1.0, 2.0), kelvin=3000)
        assert observed_to_command(obs, FULL).color is None, mode
    assert observed_to_command(on(90, "brightness"), DIMMER) == Command("on", 90)
    assert observed_to_command(on(None, "onoff"), SWITCH) == Command("on")


def test_observed_to_command_without_a_usable_colour_records_none():
    assert observed_to_command(on(90, "color_temp"), CT_ONLY) == Command("on", 90)       # CT mode, no kelvin
    assert observed_to_command(on(90, "xy"), COLOUR_ONLY) == Command("on", 90)          # colour mode, no xy/hs
    assert observed_to_command(on(90, None, xy=(0.3, 0.3)), FULL) == Command("on", 90)  # no colour mode
    assert observed_to_command(on(90, "unknown", xy=(0.3, 0.3)), FULL) == Command("on", 90)


def test_observed_to_command_clamps_an_out_of_range_brightness():
    assert observed_to_command(on(300, "brightness"), DIMMER) == Command("on", 255)


# --------------------------------------------------------------------------- #
# project
# --------------------------------------------------------------------------- #


def test_project_none_is_none():
    assert project(None, FULL) is None


def test_project_off_is_turn_off_with_no_data():
    for caps in (FULL, CT_ONLY, DIMMER, SWITCH, NO_CAPS):
        assert project(OFF_COMMAND, caps) == Call("turn_off", ())


def test_project_command_without_state_is_none():
    assert project(Command(None, 64), FULL) is None


def test_project_full_lamp():
    assert project(Command("on", 102, WARM), FULL) == turn_on(brightness=102, color_temp_kelvin=2700)
    assert project(Command("on", 255, RED_XY), FULL) == turn_on(brightness=255, xy_color=(0.64, 0.33))
    assert project(Command("on"), FULL) == Call("turn_on", ())


def test_project_brightness_only_lamp_drops_colour():
    assert project(Command("on", 128, RED_XY), DIMMER) == turn_on(brightness=128)
    assert project(Command("on", 128, WARM), DIMMER) == turn_on(brightness=128)
    assert project(Command("on", None, Color("hs_color", (14.0, 100.0))), DIMMER) == Call("turn_on", ())


def test_project_onoff_lamp_gets_no_brightness_and_no_colour():
    assert project(Command("on", 128, RED_XY), SWITCH) == Call("turn_on", ())


def test_project_unknown_caps_send_bare_turn_on():
    assert project(Command("on", 128, RED_XY), NO_CAPS) == Call("turn_on", ())


def test_project_clamps_brightness_to_1_255():
    assert project(Command("on", 0), DIMMER) == turn_on(brightness=1)
    assert project(Command("on", 1), DIMMER) == turn_on(brightness=1)
    assert project(Command("on", 255), DIMMER) == turn_on(brightness=255)


def test_project_clamps_kelvin_to_the_lamps_range():
    assert project(Command("on", 13, Color.kelvin(2000)), CT_ONLY) == turn_on(brightness=13, color_temp_kelvin=2202)
    assert project(Command("on", 13, Color.kelvin(2202)), CT_ONLY) == turn_on(brightness=13, color_temp_kelvin=2202)
    assert project(Command("on", 13, Color.kelvin(9000)), CT_ONLY) == turn_on(brightness=13, color_temp_kelvin=6500)
    assert project(Command("on", 13, WARM), CT_ONLY) == turn_on(brightness=13, color_temp_kelvin=2700)
    assert project(Command("on", None, Color.kelvin(1500)), FULL) == turn_on(color_temp_kelvin=2000)


def test_project_kelvin_with_only_one_known_bound():
    no_max = Caps(frozenset({"color_temp"}), 2202, None)
    no_range = Caps(frozenset({"color_temp"}))
    assert project(Command("on", None, Color.kelvin(9000)), no_max) == turn_on(color_temp_kelvin=9000)
    assert project(Command("on", None, Color.kelvin(1500)), no_max) == turn_on(color_temp_kelvin=2202)
    assert project(Command("on", None, Color.kelvin(1500)), no_range) == turn_on(color_temp_kelvin=1500)


def test_project_kelvin_to_a_colour_only_lamp_is_sent_as_is():
    for caps in (COLOUR_ONLY, HS_ONLY, RGBWW, Caps(frozenset({"xy"}), 3000, 4000)):
        assert project(Command("on", 50, Color.kelvin(1800)), caps) == turn_on(brightness=50, color_temp_kelvin=1800)


def test_project_kelvin_to_a_lamp_without_ct_or_colour_is_dropped():
    assert project(Command("on", 50, WARM), Caps(frozenset({"white"}))) == turn_on(brightness=50)


def test_project_xy_passes_through_exactly_as_written():
    odd = Color.xy(0.6412345, 0.3298765)
    call = project(Command("on", 255, odd), COLOUR_ONLY)
    assert call == turn_on(brightness=255, xy_color=(0.6412345, 0.3298765))
    assert call.as_dict()["xy_color"] == [0.6412345, 0.3298765]


def test_project_hs_and_rgb_are_sent_as_written_to_colour_lamps():
    hs = Color("hs_color", (14.0, 100.0))
    rgb = Color("rgb_color", (255.0, 0.0, 0.0))
    assert project(Command("on", None, hs), FULL) == turn_on(hs_color=(14.0, 100.0))
    assert project(Command("on", None, rgb), RGBWW).as_dict() == {"rgb_color": [255, 0, 0]}
    assert project(Command("on", None, rgb), WHITE).as_dict() == {"rgb_color": [255, 0, 0]}


def test_project_xy_to_a_ct_only_lamp_is_dropped():
    assert project(Command("on", 200, RED_XY), CT_ONLY) == turn_on(brightness=200)


def test_project_never_adds_transition():
    call = project(Command("on", 200, WARM), FULL)
    assert FULL.transition
    assert "transition" not in call.as_dict()


# --------------------------------------------------------------------------- #
# matches
# --------------------------------------------------------------------------- #


def test_matches_turn_off_is_state_only():
    off_call = Call.make("turn_off")
    assert matches(Observed("off"), off_call, FULL) == "yes"
    assert matches(Observed("off", 200, "xy", (0.1, 0.1)), off_call, FULL) == "yes"
    assert matches(on(200, "xy", xy=(0.1, 0.1)), off_call, FULL) == "no"
    assert matches(Observed("unavailable"), off_call, FULL) == "no"
    assert matches(Observed("unknown"), off_call, FULL) == "no"


def test_matches_turn_on_needs_on():
    assert matches(Observed("off"), turn_on(), FULL) == "no"
    assert matches(Observed("unavailable"), turn_on(brightness=5), FULL) == "no"
    assert matches(on(), turn_on(), FULL) == "yes"
    assert matches(on(3, "brightness"), Call("turn_on", ()), DIMMER) == "yes"


def test_matches_brightness_tolerance():
    call = turn_on(brightness=128)
    assert matches(on(128 + TOL_BRIGHTNESS, "brightness"), call, DIMMER) == "yes"
    assert matches(on(128 - TOL_BRIGHTNESS, "brightness"), call, DIMMER) == "yes"
    assert matches(on(128 + TOL_BRIGHTNESS + 1, "brightness"), call, DIMMER) == "no"
    assert matches(on(128 - TOL_BRIGHTNESS - 1, "brightness"), call, DIMMER) == "no"


def test_matches_brightness_not_compared_when_not_reported_or_not_requested():
    assert matches(on(None, "onoff"), turn_on(brightness=128), SWITCH) == "yes"
    assert matches(on(3, "brightness"), turn_on(), DIMMER) == "yes"


def test_matches_kelvin_in_ct_mode_within_tolerance():
    call = turn_on(brightness=102, color_temp_kelvin=2700)
    assert matches(on(102, "color_temp", kelvin=2700 + TOL_KELVIN), call, FULL) == "yes"
    assert matches(on(100, "color_temp", kelvin=2700 - TOL_KELVIN), call, FULL) == "yes"
    assert matches(on(102, "color_temp", kelvin=2700 + TOL_KELVIN + 1), call, FULL) == "colour_off"


def test_matches_kelvin_in_a_non_ct_mode_is_colour_off():
    call = turn_on(brightness=255, color_temp_kelvin=3500)
    # A release to a warm base that left the lamp in the signal colour must not verify.
    assert matches(on(255, "xy", xy=(0.64, 0.33)), call, FULL) == "colour_off"
    assert matches(on(255, "hs", hs=(28.0, 60.0), kelvin=3500), call, FULL) == "colour_off"
    assert matches(on(255, "color_temp"), call, FULL) == "colour_off"     # CT mode but no kelvin reported


def test_matches_kelvin_on_a_colour_only_lamp_compares_the_emulated_xy():
    # HA emulates kelvin on a lamp without a color_temp mode; the lamp reports the
    # xy it was given. That verifies; anything else is colour_off, as before.
    call = project(Command("on", 50, Color.kelvin(2700)), COLOUR_ONLY)
    emulated = kelvin_to_xy(2700)
    assert matches(on(50, "xy", xy=emulated), call, COLOUR_ONLY) == "yes"
    assert matches(on(50, "xy", xy=(emulated[0] + TOL_XY, emulated[1] - TOL_XY)), call, COLOUR_ONLY) == "yes"
    assert matches(on(50, "xy", xy=(0.46, 0.41)), call, COLOUR_ONLY) == "colour_off"   # the Planckian 2700 K
    assert matches(on(50, "xy", xy=(0.64, 0.33)), call, COLOUR_ONLY) == "colour_off"   # still the signal
    assert matches(on(50, "xy"), call, COLOUR_ONLY) == "colour_off"                    # no xy reported
    assert matches(on(90, "xy", xy=emulated), call, COLOUR_ONLY) == "no"
    hs_call = project(Command("on", 50, Color.kelvin(4000)), HS_ONLY)
    assert matches(on(50, "hs", hs=(29.0, 50.0), xy=kelvin_to_xy(4000)), hs_call, HS_ONLY) == "yes"


def test_matches_kelvin_on_an_rgbww_lamp_without_ct_is_not_compared():
    # HA turns it into white channels, whose xy cannot be predicted.
    call = project(Command("on", 50, Color.kelvin(2700)), RGBWW)
    assert matches(on(50, "rgbww", xy=(0.3, 0.3)), call, RGBWW) == "yes"


def test_kelvin_to_xy_is_what_home_assistant_emulates():
    # Values from homeassistant.util.color 2026.9.1: color_hs_to_xy(*color_temperature_to_hs(k)).
    expected = {
        2000: (0.598, 0.383),
        2202: (0.579, 0.388),
        2700: (0.525, 0.388),
        3000: (0.496, 0.383),
        4000: (0.42, 0.365),
        5000: (0.371, 0.349),
        6500: (0.326, 0.333),
    }
    for kelvin, xy in expected.items():
        assert kelvin_to_xy(kelvin) == xy, kelvin
    assert kelvin_to_xy(500) == kelvin_to_xy(1000)          # HA clamps to 1000-40000 K
    assert kelvin_to_xy(90000) == kelvin_to_xy(40000)


def test_compared_groups_names_what_matches_can_check():
    assert compared_groups(on(100, "xy", xy=(0.3, 0.3)), turn_on(brightness=100, xy_color=(0.3, 0.3)),
                           FULL) == {"brightness", "color"}
    assert compared_groups(on(None, "onoff"), turn_on(brightness=100), SWITCH) == frozenset()
    assert compared_groups(on(100, "hs", hs=(1.0, 2.0)), turn_on(hs_color=(1.0, 2.0)), FULL) == frozenset()
    assert compared_groups(on(100, "xy", xy=(0.3, 0.3)), turn_on(rgb_color=(1, 2, 3)), FULL) == frozenset()
    assert compared_groups(on(100, "xy", xy=(0.3, 0.3)), turn_on(color_temp_kelvin=2700), COLOUR_ONLY) == {"color"}
    assert compared_groups(on(100, "rgbww"), turn_on(color_temp_kelvin=2700), RGBWW) == frozenset()
    assert compared_groups(Observed("off"), turn_on(brightness=100), FULL) == frozenset()
    assert compared_groups(on(100), Call.make("turn_off"), FULL) == frozenset()
    assert compared_groups(None, turn_on(brightness=1), FULL) == frozenset()


def test_matches_wrong_brightness_is_no_even_with_colour_off():
    call = turn_on(brightness=255, color_temp_kelvin=3500)
    assert matches(on(100, "xy", xy=(0.64, 0.33)), call, FULL) == "no"
    xy_call = turn_on(brightness=255, xy_color=(0.64, 0.33))
    assert matches(on(100, "xy", xy=(0.1, 0.1)), xy_call, FULL) == "no"


def test_matches_xy_tolerance_on_both_axes():
    call = turn_on(brightness=255, xy_color=(0.64, 0.33))
    assert matches(on(255, "xy", xy=(0.64, 0.33)), call, FULL) == "yes"
    assert matches(on(255, "xy", xy=(0.64 + TOL_XY, 0.33 - TOL_XY)), call, FULL) == "yes"   # inclusive
    assert matches(on(255, "xy", xy=(0.662, 0.311)), call, FULL) == "yes"
    assert matches(on(255, "xy", xy=(0.64 + TOL_XY + 0.001, 0.33)), call, FULL) == "colour_off"
    assert matches(on(255, "xy", xy=(0.64, 0.33 + TOL_XY + 0.001)), call, FULL) == "colour_off"
    assert matches(on(255, "hs", hs=(15.0, 100.0)), call, FULL) == "colour_off"      # no xy reported


def test_matches_xy_compares_the_reported_xy_in_any_mode():
    call = turn_on(xy_color=(0.46, 0.41))
    assert matches(on(200, "color_temp", xy=(0.459, 0.411), kelvin=2700), call, FULL) == "yes"


def test_matches_hs_and_rgb_are_not_compared():
    assert matches(on(200, "hs", hs=(200.0, 10.0), xy=(0.2, 0.2)), turn_on(hs_color=(14.0, 100.0)), FULL) == "yes"
    assert matches(on(200, "xy", xy=(0.2, 0.2)), turn_on(rgb_color=(255, 0, 0)), FULL) == "yes"
    assert matches(on(200, "color_temp", kelvin=6500), turn_on(rgb_color=(255, 0, 0)), FULL) == "yes"


def test_matches_ignores_transition_and_unknown_services():
    call = Call.make("turn_on", {"brightness": 100, "transition": 10})
    assert matches(on(100, "brightness"), call, DIMMER) == "yes"
    assert matches(on(100, "brightness"), Call.make("toggle"), DIMMER) == "no"


def test_matches_missing_report_or_call_is_no():
    assert matches(None, turn_on(), FULL) == "no"
    assert matches(on(100), None, FULL) == "no"


def test_what_a_lamp_shows_projects_back_to_a_match():
    reports = [
        (on(102, "color_temp", xy=(0.46, 0.41), kelvin=2702), FULL),
        (on(13, "color_temp", kelvin=2202), CT_ONLY),
        (on(255, "xy", xy=(0.64, 0.33), hs=(0.0, 100.0)), COLOUR_ONLY),
        (on(40, "hs", hs=(200.0, 50.0)), HS_ONLY),
        (on(90, "brightness"), DIMMER),
        (on(None, "onoff"), SWITCH),
        (Observed("off"), FULL),
    ]
    for obs, caps in reports:
        call = project(observed_to_command(obs, caps), caps)
        assert matches(obs, call, caps) == "yes", obs


# --------------------------------------------------------------------------- #
# close
# --------------------------------------------------------------------------- #


def test_close_needs_the_same_state():
    assert close(Observed("off"), Observed("off"))
    assert close(on(100), on(100))
    assert not close(on(100), Observed("off"))
    assert not close(Observed("unavailable"), Observed("off"))


def test_close_brightness_within_tolerance_or_missing():
    assert close(on(100), on(100 + TOL_BRIGHTNESS))
    assert not close(on(100), on(100 + TOL_BRIGHTNESS + 1))
    assert close(on(100), on(None))
    assert close(on(None), on(3))


def test_close_kelvin_within_tolerance():
    assert close(on(100, "color_temp", kelvin=2700), on(100, "color_temp", kelvin=2700 + TOL_KELVIN))
    assert not close(on(100, "color_temp", kelvin=2700), on(100, "color_temp", kelvin=2700 + TOL_KELVIN + 1))


def test_close_xy_within_tolerance():
    assert close(on(255, "xy", xy=(0.64, 0.33)), on(255, "xy", xy=(0.64 + TOL_XY, 0.33)))
    assert not close(on(255, "xy", xy=(0.64, 0.33)), on(255, "xy", xy=(0.6, 0.33)))
    assert not close(on(255, "xy", xy=(0.64, 0.33)), on(255, "xy", xy=(0.64, 0.4)))


def test_close_compares_xy_across_modes():
    ct = on(200, "color_temp", xy=(0.46, 0.41), kelvin=2700)
    assert close(ct, on(200, "xy", xy=(0.47, 0.40)))
    assert not close(ct, on(200, "xy", xy=(0.64, 0.33)))


def test_close_colour_only_when_both_have_it():
    assert close(on(200, "xy", xy=(0.64, 0.33)), on(200))
    assert close(on(200, "color_temp", kelvin=2700), on(200, "brightness"))
    assert close(on(200, "hs", hs=(10.0, 100.0)), on(200, "hs", hs=(200.0, 10.0)))    # hs alone: not compared


def test_close_with_a_missing_report_is_false():
    assert not close(None, on(100))
    assert not close(on(100), None)
    assert not close(None, None)


# --------------------------------------------------------------------------- #
# raises_output
# --------------------------------------------------------------------------- #


def test_off_to_on_is_raising():
    assert raises_output(Observed("off"), Command("on", 1))
    assert raises_output(OFF_COMMAND, Command("on"))


def test_unknown_before_counts_as_off():
    assert raises_output(None, Command("on", 5))
    assert raises_output(Observed("unavailable"), Command("on", 5))
    assert raises_output(Observed("unknown"), Command("on"))
    assert raises_output(Command(None, 200), Command("on", 5))     # attributes only: state unknown


def test_brightness_increase_beyond_tolerance_is_raising():
    assert raises_output(on(100), Command("on", 100 + TOL_BRIGHTNESS + 1))
    assert raises_output(Command("on", 100), Command("on", 200))
    assert not raises_output(on(100), Command("on", 100 + TOL_BRIGHTNESS))


def test_brightness_decrease_is_not_raising():
    assert not raises_output(on(200), Command("on", 20))
    assert not raises_output(Command("on", 200, WARM), Command("on", 199, RED_XY))


def test_turning_off_is_never_raising():
    for before in (None, Observed("off"), on(10), Observed("unavailable"), OFF_COMMAND):
        assert not raises_output(before, OFF_COMMAND)
    assert not raises_output(on(10), None)


def test_on_without_brightness_keeps_the_lamp_where_it_is():
    assert not raises_output(on(10), Command("on"))
    assert not raises_output(on(None), Command("on", None, RED_XY))


def test_unknown_brightness_on_a_lit_lamp_cannot_rule_out_raising():
    assert raises_output(on(None), Command("on", 200))
    assert raises_output(Command("on"), Command("on", 1))


# --------------------------------------------------------------------------- #
# A switch: no colour modes at all (SPEC 4, added 2026-09-15)
# --------------------------------------------------------------------------- #

SWITCH_ATTRS = {"friendly_name": "Relay", "device_class": "outlet"}


def test_a_switch_has_no_modes_and_no_transition() -> None:
    caps = caps_from_attrs(SWITCH_ATTRS, "matter")
    assert caps.modes == frozenset() and not caps.transition
    assert caps.min_kelvin is None and caps.max_kelvin is None


def test_a_switch_takes_a_bare_on_or_off_whatever_the_command_says() -> None:
    caps = caps_from_attrs(SWITCH_ATTRS, "matter")
    on = project(Command("on", 200, Color.xy(0.68, 0.31)), caps)
    assert on is not None and on.service == "turn_on" and dict(on.data) == {}
    off = project(OFF_COMMAND, caps)
    assert off is not None and off.service == "turn_off"


def test_a_switch_is_verified_on_state_alone() -> None:
    caps = caps_from_attrs(SWITCH_ATTRS, "matter")
    shown_on = observed_from_state("on", SWITCH_ATTRS, 0.0)
    shown_off = observed_from_state("off", SWITCH_ATTRS, 0.0)
    assert matches(shown_on, Call.make("turn_on"), caps) == "yes"
    assert matches(shown_off, Call.make("turn_on"), caps) == "no"
    assert matches(shown_off, Call.make("turn_off"), caps) == "yes"
    assert compared_groups(shown_on, Call.make("turn_on"), caps) == frozenset()
    assert observed_to_command(shown_on, caps) == Command("on", None, None)


def test_turning_a_switch_on_raises_output_and_off_never_does() -> None:
    off = observed_from_state("off", SWITCH_ATTRS, 0.0)
    on = observed_from_state("on", SWITCH_ATTRS, 0.0)
    assert raises_output(off, Command("on", None, None))
    assert not raises_output(on, Command("on", None, None))
    assert not raises_output(on, OFF_COMMAND)
