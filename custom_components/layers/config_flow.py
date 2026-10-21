"""Config and options flow for Layers."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_BASE_KEEP,
    CONF_CLIMATE_MANUAL_PRIORITY,
    CONF_CLIMATE_MANUAL_TIMEOUT,
    CONF_CLIMATE_REPORT_WINDOW,
    CONF_DEFAULT_POLICY,
    CONF_EDIT_ACTIVE,
    CONF_ENTITIES,
    CONF_REASSERT,
    DOMAIN,
    LAMP_DOMAINS,
    MANAGED_DOMAINS,
)
from .logic.climate import (
    DEFAULT_MANUAL_PRIORITY,
    DEFAULT_MANUAL_TIMEOUT_S,
    DEFAULT_REPORT_WINDOW_S,
)
from .logic.model import DEFAULT_POLICIES, POLICY_TAKE_BACK

GROUP_ATTRS = ("entity_id", "is_hue_group", "group_entities")


def _lamps_selector(domains: frozenset[str] = MANAGED_DOMAINS) -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain=sorted(domains), multiple=True))


def _number(low: float, high: float, step: float, unit: str) -> NumberSelector:
    return NumberSelector(NumberSelectorConfig(min=low, max=high, step=step,
                                               unit_of_measurement=unit,
                                               mode=NumberSelectorMode.BOX))


def _policy_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=list(DEFAULT_POLICIES), mode=SelectSelectorMode.DROPDOWN, translation_key="policy"
        )
    )


def not_a_lamp(hass: HomeAssistant, entity_ids: list[str]) -> list[str]:
    """Entities that cannot be managed: groups of any kind, and entities with no state.

    Groups are rejected because Layers expands them itself; managing a group
    and its members at once would make every command arrive twice.
    """
    registry = er.async_get(hass)
    bad = []
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        entry = registry.async_get(entity_id)
        if (
            state is None
            or any(attr in state.attributes for attr in GROUP_ATTRS)
            or (entry is not None and entry.platform == "group")
        ):
            bad.append(entity_id)
    return bad


class LayersConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Layers from the UI."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            bad = not_a_lamp(self.hass, user_input[CONF_ENTITIES])
            if bad:
                errors[CONF_ENTITIES] = "not_a_lamp"
                placeholders["entities"] = ", ".join(bad)
            else:
                return self.async_create_entry(
                    title="Layers",
                    data={},
                    options={
                        CONF_ENTITIES: user_input[CONF_ENTITIES],
                        CONF_DEFAULT_POLICY: user_input[CONF_DEFAULT_POLICY],
                        CONF_EDIT_ACTIVE: [],
                        CONF_BASE_KEEP: [],
                        CONF_REASSERT: [],
                    },
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_ENTITIES): _lamps_selector(),
                vol.Required(CONF_DEFAULT_POLICY, default=POLICY_TAKE_BACK): _policy_selector(),
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors, description_placeholders=placeholders
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> LayersOptionsFlow:
        return LayersOptionsFlow()


class LayersOptionsFlow(OptionsFlowWithReload):
    """Change the managed lamps and their policies. Saving reloads the entry."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = self.config_entry.options
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            lamps = user_input[CONF_ENTITIES]
            edit = user_input.get(CONF_EDIT_ACTIVE, [])
            keep = user_input.get(CONF_BASE_KEEP, [])
            reassert = user_input.get(CONF_REASSERT, [])
            removed = set(options.get(CONF_ENTITIES, [])) - set(lamps)
            holding = self._holding_layers(removed)
            if bad := not_a_lamp(self.hass, lamps):
                errors[CONF_ENTITIES] = "not_a_lamp"
                placeholders["entities"] = ", ".join(bad)
            elif holding:
                # Un-enrolling a held lamp would strand it: nothing would ever release it.
                errors[CONF_ENTITIES] = "holds_layers"
                placeholders["entities"] = ", ".join(sorted(holding))
            elif stray := sorted((set(edit) | set(keep) | set(reassert)) - set(lamps)):
                field = (CONF_EDIT_ACTIVE if set(edit) - set(lamps)
                         else CONF_BASE_KEEP if set(keep) - set(lamps) else CONF_REASSERT)
                errors[field] = "not_enrolled"
                placeholders["entities"] = ", ".join(stray)
            elif both := sorted((set(edit) & set(keep)) | (set(edit) & set(reassert))
                                | (set(keep) & set(reassert))):
                errors[CONF_BASE_KEEP] = "both_policies"
                placeholders["entities"] = ", ".join(both)
            else:
                return self.async_create_entry(
                    data={
                        CONF_ENTITIES: lamps,
                        CONF_DEFAULT_POLICY: user_input[CONF_DEFAULT_POLICY],
                        CONF_EDIT_ACTIVE: edit,
                        CONF_BASE_KEEP: keep,
                        CONF_REASSERT: reassert,
                        CONF_CLIMATE_MANUAL_PRIORITY: int(user_input.get(
                            CONF_CLIMATE_MANUAL_PRIORITY, DEFAULT_MANUAL_PRIORITY)),
                        CONF_CLIMATE_MANUAL_TIMEOUT: int(user_input.get(
                            CONF_CLIMATE_MANUAL_TIMEOUT, DEFAULT_MANUAL_TIMEOUT_S / 60)),
                        CONF_CLIMATE_REPORT_WINDOW: int(user_input.get(
                            CONF_CLIMATE_REPORT_WINDOW, DEFAULT_REPORT_WINDOW_S)),
                    }
                )
        current = user_input or options
        schema = vol.Schema(
            {
                vol.Required(CONF_ENTITIES, default=current.get(CONF_ENTITIES, [])): _lamps_selector(),
                vol.Required(
                    CONF_DEFAULT_POLICY, default=current.get(CONF_DEFAULT_POLICY, POLICY_TAKE_BACK)
                ): _policy_selector(),
                vol.Optional(CONF_EDIT_ACTIVE, default=current.get(CONF_EDIT_ACTIVE, [])):
                    _lamps_selector(LAMP_DOMAINS),
                vol.Optional(CONF_BASE_KEEP, default=current.get(CONF_BASE_KEEP, [])):
                    _lamps_selector(LAMP_DOMAINS),
                vol.Optional(CONF_REASSERT, default=current.get(CONF_REASSERT, [])):
                    _lamps_selector(LAMP_DOMAINS),
                vol.Optional(CONF_CLIMATE_MANUAL_PRIORITY, default=current.get(
                    CONF_CLIMATE_MANUAL_PRIORITY, DEFAULT_MANUAL_PRIORITY)): _number(1, 99, 1, ""),
                vol.Optional(CONF_CLIMATE_MANUAL_TIMEOUT, default=current.get(
                    CONF_CLIMATE_MANUAL_TIMEOUT, int(DEFAULT_MANUAL_TIMEOUT_S / 60))):
                    _number(0, 7 * 24 * 60, 1, "min"),
                vol.Optional(CONF_CLIMATE_REPORT_WINDOW, default=current.get(
                    CONF_CLIMATE_REPORT_WINDOW, int(DEFAULT_REPORT_WINDOW_S))):
                    _number(5, 600, 1, "s"),
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors, description_placeholders=placeholders
        )

    def _holding_layers(self, entity_ids: set[str]) -> set[str]:
        if not entity_ids or self.config_entry.state is not ConfigEntryState.LOADED:
            return set()
        engine = self.config_entry.runtime_data.engine
        return {e for e in entity_ids if engine.holds_layers(e)}
