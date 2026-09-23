"""The members a scene skips because they already look the way it wants.

Home Assistant's scenes send nothing to a member that already matches: light and
switch ``reproduce_state`` return early. So no service call and no state change
reaches Layers for that member. If a layer is what made it match (a layer holds
the ceiling off and a bedtime scene also turns it off), the layer outlives the
scene, and clearing it later puts the old base back: the scene is undone.

The engine asks this module what a scene would have sent each enrolled member,
and treats a member that got no call under the scene's context as if it had been
sent exactly that. The classifier's intent path (SPEC 6.3) then decides, as for
any call that changes nothing visible: it is only recorded when the lamp really
shows it. A scene that stopped halfway cannot claim a lamp it never reached.

Only Home Assistant's own scenes (YAML, the scene editor, ``scene.create``) and
``scene.apply`` list their members. A vendor scene (a Hue bridge scene, say) is
run by the bridge; its lamps report their changes and the state path sees them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import voluptuous as vol
from homeassistant.const import ATTR_ENTITY_ID, ENTITY_MATCH_ALL, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids

SCENE_DOMAIN = "scene"
SCENE_SERVICES = frozenset({"turn_on", "apply"})
ATTR_COLOR_MODE = "color_mode"
CONF_ENTITIES = "entities"

# What light reproduce_state sends for a state, mirrored here: the shape of its own
# tables changed between Home Assistant releases (plain names in 2026.7, pairs later),
# while the names themselves did not. A service parameter has its state attribute's name
# except in white mode, which sends ``white`` at the state's brightness.
_ATTR_GROUP = ("brightness", "effect")
_COLOR_GROUP = ("hs_color", "color_temp_kelvin", "rgb_color", "rgbw_color", "rgbww_color",
                "xy_color")
_BY_COLOR_MODE = {                   # color_mode -> (service parameter, state attribute)
    "color_temp": ("color_temp_kelvin", "color_temp_kelvin"),
    "hs": ("hs_color", "hs_color"),
    "rgb": ("rgb_color", "rgb_color"),
    "rgbw": ("rgbw_color", "rgbw_color"),
    "rgbww": ("rgbww_color", "rgbww_color"),
    "white": ("white", "brightness"),
    "xy": ("xy_color", "xy_color"),
}
_UNKNOWN_MODE = "unknown"


def scene_targets(
    hass: HomeAssistant, service: str, data: Mapping[str, Any], enrolled: Iterable[str]
) -> dict[str, State]:
    """What a ``scene.turn_on`` or ``scene.apply`` call wants of each enrolled lamp."""
    enrolled = set(enrolled)
    if service == "apply":
        wanted = _apply_states(data.get(CONF_ENTITIES))
    else:
        wanted = {}
        for scene in _scene_entities(hass, data):
            wanted.update(_members(scene))
    return {eid: state for eid, state in wanted.items() if eid in enrolled}


def reproduced_call(state: State) -> tuple[str, dict[str, Any]] | None:
    """The service and data Home Assistant's scene would send to reach ``state``.

    Mirrors light and switch ``reproduce_state``. ``None`` when it would send
    nothing even to a lamp that does not match (an invalid state, or a colour mode
    whose colour is missing), so there is no intent to record.
    """
    if state.state == STATE_OFF:
        return "turn_off", {}
    if state.state != STATE_ON:
        return None
    if state.domain != "light":
        return "turn_on", {}
    data: dict[str, Any] = {}
    attrs = state.attributes
    for attribute in _ATTR_GROUP:
        if (value := attrs.get(attribute)) is not None:
            data[attribute] = value
    mode = attrs.get(ATTR_COLOR_MODE, _UNKNOWN_MODE)
    if mode != _UNKNOWN_MODE:
        if (by_mode := _BY_COLOR_MODE.get(str(mode))) is not None:
            parameter, attribute = by_mode
            if (value := attrs.get(attribute)) is None:
                return None
            data[parameter] = value
    else:
        for attribute in _COLOR_GROUP:
            if (value := attrs.get(attribute)) is not None:
                data[attribute] = value
                break
    return "turn_on", data


def _scene_entities(hass: HomeAssistant, data: Mapping[str, Any]) -> list[Any]:
    component = hass.data.get(SCENE_DOMAIN)
    if component is None or not hasattr(component, "get_entity"):
        return []
    raw = dict(data)
    entity_ids = raw.get(ATTR_ENTITY_ID)
    if entity_ids is not None:
        try:
            entity_ids = raw[ATTR_ENTITY_ID] = cv.comp_entity_ids(entity_ids)
        except vol.Invalid:
            return []
    if entity_ids == ENTITY_MATCH_ALL:
        return list(component.entities)
    selected = async_extract_referenced_entity_ids(hass, TargetSelection(raw), expand_group=True)
    scene_ids = sorted(
        eid for eid in selected.referenced | selected.indirectly_referenced
        if eid.split(".", 1)[0] == SCENE_DOMAIN
    )
    return [scene for eid in scene_ids if (scene := component.get_entity(eid)) is not None]


def _members(scene: Any) -> dict[str, State]:
    """A Home Assistant scene's members; a vendor scene has none Layers can read."""
    states = getattr(getattr(scene, "scene_config", None), "states", None)
    if not isinstance(states, Mapping):
        return {}
    return {eid: state for eid, state in states.items() if isinstance(state, State)}


def _apply_states(entities: Any) -> dict[str, State]:
    """``scene.apply``'s ``entities``, as its schema reads them: a state, or a dict of
    ``state`` plus attributes (YAML's ``on``/``off`` arrive as booleans)."""
    if not isinstance(entities, Mapping):
        return {}
    wanted: dict[str, State] = {}
    for raw_id, value in entities.items():
        if isinstance(value, State):
            wanted[value.entity_id] = value
            continue
        if isinstance(value, Mapping):
            attrs = dict(value)
            state = attrs.pop("state", None)
        else:
            state, attrs = value, {}
        if isinstance(state, bool):
            state = STATE_ON if state else STATE_OFF
        if not isinstance(state, str):
            continue
        try:
            eid = cv.entity_id(raw_id)
            wanted[eid] = State(eid, state, attrs)
        except Exception:  # noqa: BLE001 — an entity id or state Home Assistant would refuse too
            continue
    return wanted
