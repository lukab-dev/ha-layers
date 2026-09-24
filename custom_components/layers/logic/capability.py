"""What a lamp can take, what it reports, and whether it took what it was sent.

Pure: the Home Assistant side hands in a state string and a plain attribute
mapping, and gets back model types. Colour modes are compared as the strings
Home Assistant uses for ``ColorMode`` (a ``StrEnum``), so no import is needed.

SPEC section 4:

- ``caps_from_attrs`` / ``observed_from_state`` read a light's attributes;
- ``observed_to_command`` records what a lamp shows as a command;
- ``project`` turns an effective command into the call a lamp can take;
- ``matches`` verifies a call against a report (``yes`` / ``no`` / ``colour_off``);
- ``compared_groups`` says which attribute groups ``matches`` could check;
- ``close`` compares two reports within tolerance;
- ``raises_output`` answers the expiry safety rule's question;
- ``kelvin_to_xy`` is the xy Home Assistant gives a colour temperature it emulates.
"""

from __future__ import annotations

import colorsys
import math
from collections.abc import Mapping
from typing import Any, Literal

from .model import (
    COLOR_HS,
    COLOR_KELVIN,
    COLOR_XY,
    GROUP_BRIGHTNESS,
    GROUP_COLOR,
    NO_STATE,
    OFF,
    OFF_COMMAND,
    ON,
    TOL_BRIGHTNESS,
    TOL_KELVIN,
    TOL_XY,
    Call,
    Caps,
    Color,
    Command,
    Observed,
)

# Colour modes, spelled as Home Assistant's ColorMode values.
CM_ONOFF = "onoff"
CM_BRIGHTNESS = "brightness"
CM_COLOR_TEMP = "color_temp"
CM_HS = "hs"
CM_XY = "xy"
CM_RGB = "rgb"
CM_RGBW = "rgbw"
CM_RGBWW = "rgbww"
CM_WHITE = "white"
COLOUR_MODES = frozenset({CM_HS, CM_XY, CM_RGB, CM_RGBW, CM_RGBWW})
# Every mode that implies dimming: all but onoff (and HA's "unknown"), as HA's brightness_supported().
BRIGHTNESS_MODES = COLOUR_MODES | {CM_BRIGHTNESS, CM_COLOR_TEMP, CM_WHITE}

SUPPORT_TRANSITION = 32     # LightEntityFeature.TRANSITION

SERVICE_TURN_ON = "turn_on"
SERVICE_TURN_OFF = "turn_off"

ATTR_SUPPORTED_COLOR_MODES = "supported_color_modes"
ATTR_SUPPORTED_FEATURES = "supported_features"
ATTR_MIN_KELVIN = "min_color_temp_kelvin"
ATTR_MAX_KELVIN = "max_color_temp_kelvin"
ATTR_BRIGHTNESS = "brightness"
ATTR_COLOR_MODE = "color_mode"
ATTR_XY_COLOR = COLOR_XY
ATTR_HS_COLOR = COLOR_HS
ATTR_COLOR_TEMP_KELVIN = COLOR_KELVIN

MATCH_YES = "yes"
MATCH_NO = "no"
MATCH_COLOUR_OFF = "colour_off"     # state and brightness right, colour not (see DIV_COLOUR)
MatchResult = Literal["yes", "no", "colour_off"]

# Slack for float noise when a difference lands exactly on a tolerance (0.67 - 0.64 > 0.03).
_EPS = 1e-9


# --------------------------------------------------------------------------- #
# Reading attributes
# --------------------------------------------------------------------------- #


def _to_int(value: Any) -> int | None:
    """An attribute as an int, or ``None`` when missing or not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        return None


def _to_pair(value: Any) -> tuple[float, float] | None:
    """A two-number attribute (xy, hs) as a tuple of floats, or ``None``."""
    if value is None or isinstance(value, str | bytes):
        return None
    try:
        items = [float(v) for v in value]
    except (TypeError, ValueError):
        return None
    if len(items) != 2:
        return None
    return (items[0], items[1])


def _mode_name(mode: Any) -> str:
    """A colour mode as its plain string (ColorMode is a StrEnum; its value is the string)."""
    return str(getattr(mode, "value", mode))


def supports_brightness(caps: Caps) -> bool:
    """The lamp has a mode that dims (anything but onoff)."""
    return bool(caps.modes & BRIGHTNESS_MODES)


def supports_colour(caps: Caps) -> bool:
    """The lamp has a mode that takes xy / hs / rgb."""
    return bool(caps.modes & COLOUR_MODES)


def supports_color_temp(caps: Caps) -> bool:
    """The lamp has a native colour-temperature mode."""
    return CM_COLOR_TEMP in caps.modes


def caps_from_attrs(attrs: Mapping[str, Any], platform: str) -> Caps:
    """A lamp's capabilities from its state attributes.

    ``supported_color_modes`` becomes ``modes``; the kelvin range is read from
    ``min_color_temp_kelvin`` / ``max_color_temp_kelvin``; ``transition`` is
    bit 32 of ``supported_features``. Missing attributes give an empty mode
    set, no range and no transition - which is exactly what a ``switch.*``
    entity has, so a switch is a lamp that only does on/off: ``project`` drops
    brightness and colour for it, and ``matches`` compares its state alone.
    """
    attrs = attrs or {}
    raw = attrs.get(ATTR_SUPPORTED_COLOR_MODES) or ()
    if isinstance(raw, str):
        raw = (raw,)
    features = _to_int(attrs.get(ATTR_SUPPORTED_FEATURES)) or 0
    return Caps(
        modes=frozenset(_mode_name(m) for m in raw),
        min_kelvin=_to_int(attrs.get(ATTR_MIN_KELVIN)),
        max_kelvin=_to_int(attrs.get(ATTR_MAX_KELVIN)),
        transition=bool(features & SUPPORT_TRANSITION),
        platform=platform,
    )


def observed_from_state(state: str, attrs: Mapping[str, Any], at: float) -> Observed:
    """A lamp's report reduced to what Layers compares.

    Attributes are taken as reported (ints and float tuples); a missing or
    malformed one is ``None``. An unavailable lamp simply has none of them.
    """
    attrs = attrs or {}
    mode = attrs.get(ATTR_COLOR_MODE)
    return Observed(
        state=str(state),
        brightness=_to_int(attrs.get(ATTR_BRIGHTNESS)),
        color_mode=_mode_name(mode) if mode is not None else None,
        xy=_to_pair(attrs.get(ATTR_XY_COLOR)),
        hs=_to_pair(attrs.get(ATTR_HS_COLOR)),
        kelvin=_to_int(attrs.get(ATTR_COLOR_TEMP_KELVIN)),
        at=float(at),
    )


# --------------------------------------------------------------------------- #
# Recording and projecting
# --------------------------------------------------------------------------- #


def _observed_colour(obs: Observed) -> Color | None:
    """The colour a lamp shows, in the form it can be sent back: kelvin in CT mode, else xy."""
    mode = obs.color_mode
    if mode == CM_COLOR_TEMP:
        return Color.kelvin(obs.kelvin) if obs.kelvin is not None else None
    if mode in COLOUR_MODES:
        if obs.xy is not None:
            return Color.xy(*obs.xy)
        if obs.hs is not None:
            return Color(COLOR_HS, (float(obs.hs[0]), float(obs.hs[1])))
    return None     # brightness / onoff / white / unknown: no colour


def observed_to_command(obs: Observed | None, caps: Caps) -> Command | None:
    """What a lamp shows, as a command (used to record a base from a report).

    Unavailable, unknown or missing -> ``None``. Off -> ``Command(off)``. On ->
    brightness plus the colour its ``color_mode`` says it is showing:
    ``color_temp`` -> kelvin; ``xy``/``hs``/``rgb``/``rgbw``/``rgbww`` -> the
    reported xy (falling back to hs); ``brightness``/``onoff``/``white`` -> no
    colour. ``caps`` is not consulted: the report already says what the lamp
    shows, and ``project`` adapts it to the lamp when it is sent.
    """
    if obs is None or obs.state in NO_STATE:
        return None
    if obs.state == OFF:
        return OFF_COMMAND
    if obs.state != ON:
        return None
    brightness = None if obs.brightness is None else max(0, min(255, obs.brightness))
    return Command(ON, brightness, _observed_colour(obs))


def _project_colour(color: Color | None, caps: Caps) -> tuple[str, Any] | None:
    """The colour field a lamp can take, or ``None`` when it would be dropped."""
    if color is None:
        return None
    if color.key == COLOR_KELVIN:
        kelvin = int(color.service_value())
        if supports_color_temp(caps):
            if caps.min_kelvin is not None:
                kelvin = max(kelvin, caps.min_kelvin)
            if caps.max_kelvin is not None:
                kelvin = min(kelvin, caps.max_kelvin)
            return COLOR_KELVIN, kelvin
        if supports_colour(caps):
            return COLOR_KELVIN, kelvin     # as is: Home Assistant emulates it on a colour lamp
        return None
    if supports_colour(caps):
        return color.key, color.service_value()     # exactly as written
    return None


def project(cmd: Command | None, caps: Caps) -> Call | None:
    """The light service call an effective command becomes on this lamp.

    ``None`` -> ``None`` (do nothing), and so is a command without a state,
    which is never an effective command. Off -> ``turn_off`` with no data. On ->
    ``turn_on`` with brightness clamped to 1-255 if the lamp dims, and the
    colour the lamp can take: kelvin is clamped to a CT lamp's range, sent as
    is to a colour-only lamp and dropped otherwise; xy/hs/rgb go as written to
    a colour lamp and are dropped otherwise. No transition: the renderer adds it.
    """
    if cmd is None or cmd.state is None:
        return None
    if cmd.state == OFF:
        return Call.make(SERVICE_TURN_OFF)
    data: dict[str, Any] = {}
    if cmd.brightness is not None and supports_brightness(caps):
        data[ATTR_BRIGHTNESS] = max(1, min(255, cmd.brightness))
    colour = _project_colour(cmd.color, caps)
    if colour is not None:
        data[colour[0]] = colour[1]
    return Call.make(SERVICE_TURN_ON, data)


# --------------------------------------------------------------------------- #
# Comparing
# --------------------------------------------------------------------------- #


def _within(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol + _EPS


def _xy_within(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return len(a) == 2 and len(b) == 2 and _within(a[0], b[0], TOL_XY) and _within(a[1], b[1], TOL_XY)


def _helland_rgb(kelvin: float) -> tuple[float, float, float]:
    """T. Helland's colour temperature -> RGB, as Home Assistant's ``color_temperature_to_rgb``."""
    t = min(max(kelvin, 1000.0), 40000.0) / 100.0

    def clamp(value: float) -> float:
        return min(max(value, 0.0), 255.0)

    red = 255.0 if t <= 66 else clamp(329.698727446 * math.pow(t - 60, -0.1332047592))
    if t <= 66:
        green = clamp(99.4708025861 * math.log(t) - 161.1195681661)
    else:
        green = clamp(288.1221695283 * math.pow(t - 60, -0.0755148492))
    if t >= 66:
        blue = 255.0
    elif t <= 19:
        blue = 0.0
    else:
        blue = clamp(138.5177312231 * math.log(t - 10) - 305.0447927307)
    return red, green, blue


def kelvin_to_xy(kelvin: float) -> tuple[float, float]:
    """The xy Home Assistant gives a colour temperature on a lamp without a ``color_temp`` mode.

    Home Assistant emulates kelvin through hs there, and derives a CT-mode
    lamp's ``xy_color`` the same way: ``color_hs_to_xy(*color_temperature_to_hs(k))``.
    This mirrors that chain exactly (Helland RGB, hs rounded to 3 decimals,
    RGB rounded, then the Wide RGB D65 xy rounded to 3 decimals). It is not the
    Planckian locus: 2700 K comes out near (0.525, 0.388), not (0.460, 0.411).
    """
    red, green, blue = _helland_rgb(kelvin)
    hue, sat, _ = colorsys.rgb_to_hsv(red / 255.0, green / 255.0, blue / 255.0)
    hue, sat = round(hue * 360, 3), round(sat * 100, 3)
    rgb = [round(c * 255) for c in colorsys.hsv_to_rgb(hue / 360, sat / 100, 1.0)]
    if sum(rgb) == 0:
        return (0.0, 0.0)

    def linear(c: int) -> float:
        v = c / 255
        return math.pow((v + 0.055) / 1.055, 2.4) if v > 0.04045 else v / 12.92

    r, g, b = (linear(c) for c in rgb)
    x = r * 0.664511 + g * 0.154324 + b * 0.162028
    y = r * 0.283881 + g * 0.668433 + b * 0.047685
    z = r * 0.000088 + g * 0.072310 + b * 0.986039
    total = x + y + z
    return (round(x / total, 3), round(y / total, 3))


def _kelvin_shown(obs: Observed, kelvin: float, caps: Caps) -> bool | None:
    """Does ``obs`` show ``kelvin``? ``None`` when it cannot be told.

    - In ``color_temp`` mode: the reported kelvin within ``TOL_KELVIN``.
    - A lamp with a ``color_temp`` mode that reports another mode did not take it.
    - A lamp without one: Home Assistant emulated it, so the reported xy within
      ``TOL_XY`` of ``kelvin_to_xy``. An ``rgbww`` lamp gets white channels
      instead, whose xy cannot be predicted: not compared.
    """
    if obs.color_mode == CM_COLOR_TEMP:
        return obs.kelvin is not None and _within(obs.kelvin, kelvin, TOL_KELVIN)
    if supports_color_temp(caps):
        return False
    if CM_RGBWW in caps.modes:
        return None
    return obs.xy is not None and _xy_within(obs.xy, kelvin_to_xy(kelvin))


def matches(obs: Observed | None, call: Call | None, caps: Caps) -> MatchResult:
    """Did the lamp take ``call``? ``yes``, ``no``, or ``colour_off``.

    - The state must match the service (``turn_off`` <-> off, ``turn_on`` <-> on);
      a ``turn_off`` is ``yes`` on state alone.
    - Brightness must be within ``TOL_BRIGHTNESS`` when the call has it and the
      lamp reports it.
    - Kelvin: in ``color_temp`` mode, within ``TOL_KELVIN``; a lamp that has a
      ``color_temp`` mode but reports another did not take it; a lamp without
      one (Home Assistant emulates kelvin there) must report the xy of
      ``kelvin_to_xy`` within ``TOL_XY`` (an ``rgbww`` one is not compared).
    - xy: the reported xy within ``TOL_XY`` on both axes. hs and rgb are not
      compared. A colour that is compared and wrong is ``colour_off``.
    - A wrong state or brightness is ``no``, whatever the colour.

    Keys the comparison does not know (e.g. ``transition``) are ignored. A
    missing report or call is ``no``. ``caps`` is read only for kelvin.
    """
    if obs is None or call is None:
        return MATCH_NO
    if call.service == SERVICE_TURN_OFF:
        return MATCH_YES if obs.state == OFF else MATCH_NO
    if call.service != SERVICE_TURN_ON or obs.state != ON:
        return MATCH_NO

    data = dict(call.data)
    brightness = data.get(ATTR_BRIGHTNESS)
    if (
        brightness is not None
        and obs.brightness is not None
        and not _within(obs.brightness, brightness, TOL_BRIGHTNESS)
    ):
        return MATCH_NO

    kelvin = data.get(COLOR_KELVIN)
    if kelvin is not None and _kelvin_shown(obs, kelvin, caps) is False:
        return MATCH_COLOUR_OFF
    xy = data.get(COLOR_XY)
    if xy is not None and (obs.xy is None or not _xy_within(obs.xy, tuple(xy))):
        return MATCH_COLOUR_OFF
    return MATCH_YES


def compared_groups(obs: Observed | None, call: Call | None, caps: Caps) -> frozenset[str]:
    """The attribute groups ``matches`` actually checks for this report and call.

    ``brightness`` when the call has it and the lamp reports one; ``color`` for
    xy, and for kelvin unless it cannot be told (see ``matches``). hs and rgb
    are never compared. Empty for anything but a ``turn_on`` on a lamp that is on.
    """
    if obs is None or call is None or call.service != SERVICE_TURN_ON or obs.state != ON:
        return frozenset()
    data = dict(call.data)
    groups: set[str] = set()
    if data.get(ATTR_BRIGHTNESS) is not None and obs.brightness is not None:
        groups.add(GROUP_BRIGHTNESS)
    kelvin = data.get(COLOR_KELVIN)
    if data.get(COLOR_XY) is not None or (
        kelvin is not None and _kelvin_shown(obs, kelvin, caps) is not None
    ):
        groups.add(GROUP_COLOR)
    return frozenset(groups)


def close(a: Observed | None, b: Observed | None) -> bool:
    """Two reports show the same thing, within tolerance.

    Same state; brightness within ``TOL_BRIGHTNESS`` (or either missing); and,
    when both carry one, the same colour: kelvin within ``TOL_KELVIN`` when both
    report kelvin, else xy within ``TOL_XY`` when both report xy. hs alone is
    not compared. A missing report is never close.
    """
    if a is None or b is None:
        return False
    if a.state != b.state:
        return False
    if (
        a.brightness is not None
        and b.brightness is not None
        and not _within(a.brightness, b.brightness, TOL_BRIGHTNESS)
    ):
        return False
    if a.kelvin is not None and b.kelvin is not None:
        return _within(a.kelvin, b.kelvin, TOL_KELVIN)
    if a.xy is not None and b.xy is not None:
        return _xy_within(a.xy, b.xy)
    return True


def raises_output(before: Observed | Command | None, after: Command | None) -> bool:
    """Would going from ``before`` to ``after`` light or brighten the lamp?

    ``True`` if ``after`` is on and ``before`` is off or unknown (``None``,
    unavailable, or a command without a state), or if ``after`` raises the
    brightness by more than ``TOL_BRIGHTNESS``. When the lamp is on at an
    unknown brightness, an ``after`` that sets one counts as raising: the
    safety rule cannot rule it out. Colour changes never count.
    """
    if after is None or after.state != ON:
        return False
    if before is None or before.state != ON:
        return True
    if after.brightness is None:
        return False
    if before.brightness is None:
        return True
    return after.brightness - before.brightness > TOL_BRIGHTNESS
