"""Resolve service targets and other integrations' light calls to enrolled lamps.

Home Assistant's own target helper does not expand light groups or vendor room
groups (it only expands entities that carry a ``group`` attribute and old-style
``group.*`` entities, which it is left to do here too, as the light service
does), so light groups are expanded here, recursively, through their
``entity_id`` attribute. Members without a state (a disabled integration's
leftovers) are dropped.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID, ENTITY_MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids

from .logic.model import (
    GROUP_BRIGHTNESS,
    GROUP_COLOR,
    GROUP_STATE,
    OFF,
    ON,
    Color,
    Command,
)

# Light call fields whose effect cannot be known from the call alone. A call that
# uses any of them still attributes the state change to its caller, but gives no
# intent: the lamp's reported state is what counts.
_UNKNOWN_INTENT = frozenset(
    {"brightness_step", "brightness_step_pct", "profile", "flash", "color_name", "white",
     "rgbw_color", "rgbww_color", "effect"}
)


def _is_group(hass: HomeAssistant, entity_id: str, attrs: Mapping[str, Any]) -> bool:
    return "entity_id" in attrs or "group_entities" in attrs


def _is_vendor_group(hass: HomeAssistant, entity_id: str, attrs: Mapping[str, Any]) -> bool:
    """A group the vendor's hub fans out itself: its members change with no context."""
    if "is_hue_group" in attrs:
        return True
    entry = er.async_get(hass).async_get(entity_id)
    return entry is not None and entry.platform != "group" and _is_group(hass, entity_id, attrs)


def expand(
    hass: HomeAssistant, entity_ids: Iterable[str]
) -> tuple[set[str], set[str]]:
    """Expand light groups to member lamps.

    Returns ``(lamps, via_vendor_group)``: every member lamp reached, and the
    subset reached through a vendor room/zone group.
    """
    lamps: set[str] = set()
    via_vendor: set[str] = set()
    seen: set[str] = set()
    stack: list[tuple[str, bool]] = [(e, False) for e in entity_ids]
    while stack:
        entity_id, vendor = stack.pop()
        if entity_id in seen or not entity_id.startswith("light."):
            continue
        seen.add(entity_id)
        state = hass.states.get(entity_id)
        if state is None:
            continue
        attrs = state.attributes
        if _is_group(hass, entity_id, attrs):
            members = attrs.get("entity_id") or attrs.get("group_entities") or []
            is_vendor = vendor or _is_vendor_group(hass, entity_id, attrs)
            stack.extend((m, is_vendor) for m in members)
            continue
        lamps.add(entity_id)
        if vendor:
            via_vendor.add(entity_id)
    return lamps, via_vendor


def service_targets(hass: HomeAssistant, data: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """Lamps a layers.* call targets: ``(explicitly named, reached indirectly)``.

    Groups named explicitly count as explicit; lamps reached through an area,
    label, floor or device are indirect (not being enrolled is then not worth
    reporting).
    """
    selection = TargetSelection(dict(data))
    selected = async_extract_referenced_entity_ids(hass, selection, expand_group=True)
    explicit, _ = expand(hass, selected.referenced)
    indirect, _ = expand(hass, selected.indirectly_referenced)
    return explicit, indirect - explicit


def normalise_call(
    hass: HomeAssistant, data: Mapping[str, Any], service: str, enrolled: set[str]
) -> tuple[frozenset[str], frozenset[str], Command | None, frozenset[str]]:
    """Reduce someone else's light.* call to what Layers needs.

    Returns ``(lamps, via_vendor_group, intent, groups)``: the enrolled lamps it
    targets, the subset reached through a vendor group, the command it asks for
    (``None`` if it cannot be known: toggle, brightness steps, profiles, ...),
    and the attribute groups it specifies.
    """
    raw = dict(data)
    entity_ids = raw.get(ATTR_ENTITY_ID)
    if entity_ids is not None:
        # As the light service reads it: "ALL" is all, a comma list is a list.
        try:
            entity_ids = raw[ATTR_ENTITY_ID] = cv.comp_entity_ids(entity_ids)
        except vol.Invalid:
            entity_ids = raw[ATTR_ENTITY_ID] = []
    if entity_ids == ENTITY_MATCH_ALL:
        lamps, via = set(enrolled), set()
    else:
        selection = TargetSelection(raw)
        # expand_group: old-style group.* entities reach their members, as in the
        # light service itself; light groups and vendor rooms are expanded below.
        selected = async_extract_referenced_entity_ids(hass, selection, expand_group=True)
        lamps, via = expand(hass, selected.referenced | selected.indirectly_referenced)
    lamps &= enrolled
    via &= lamps

    intent: Command | None = None
    groups: frozenset[str] = frozenset()
    if service == "turn_off":
        intent, groups = Command(OFF), frozenset({GROUP_STATE})
    elif service == "turn_on" and not (_UNKNOWN_INTENT & raw.keys()):
        brightness = raw.get("brightness")
        if brightness is None and raw.get("brightness_pct") is not None:
            brightness = round(255 * float(raw["brightness_pct"]) / 100)
        if brightness is not None and int(brightness) == 0:
            intent, groups = Command(OFF), frozenset({GROUP_STATE})
        else:
            color = _color(raw)
            intent = Command(ON, None if brightness is None else int(brightness), color)
            g = {GROUP_STATE}
            if brightness is not None:
                g.add(GROUP_BRIGHTNESS)
            if color is not None:
                g.add(GROUP_COLOR)
            groups = frozenset(g)
    return frozenset(lamps), frozenset(via), intent, groups


def _color(raw: Mapping[str, Any]) -> Color | None:
    try:
        if raw.get("xy_color") is not None:
            x, y = raw["xy_color"]
            return Color.xy(x, y)
        if raw.get("hs_color") is not None:
            h, s = raw["hs_color"]
            return Color("hs_color", (float(h), float(s)))
        if raw.get("rgb_color") is not None:
            r, g, b = raw["rgb_color"]
            return Color("rgb_color", (float(r), float(g), float(b)))
        if raw.get("color_temp_kelvin") is not None:
            return Color.kelvin(raw["color_temp_kelvin"])
    except (TypeError, ValueError):
        return None
    return None
