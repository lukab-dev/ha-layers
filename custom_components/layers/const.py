"""Constants for the Layers integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "layers"

# What can be enrolled. A light is the full model; a switch is a lamp that only does
# on/off (no colour modes, so brightness and colour are projected away). Groups of
# either kind are expanded, never enrolled.
MANAGED_DOMAINS: Final = frozenset({"light", "switch"})

STORAGE_KEY: Final = "layers.state"
STORAGE_VERSION: Final = 1
STORAGE_MINOR_VERSION: Final = 1

# Config entry options
CONF_ENTITIES: Final = "entities"
CONF_DEFAULT_POLICY: Final = "default_policy"
CONF_EDIT_ACTIVE: Final = "edit_active_entities"
CONF_BASE_KEEP: Final = "base_keep_layers_entities"
CONF_REASSERT: Final = "reassert_entities"

# Services
SERVICE_SET: Final = "set"
SERVICE_CLEAR: Final = "clear"
SERVICE_SYNC: Final = "sync"
SERVICE_GET: Final = "get"

# Service fields
ATTR_LAYER: Final = "layer"
ATTR_PRIORITY: Final = "priority"
ATTR_MODE: Final = "mode"
ATTR_STATE: Final = "state"
ATTR_TTL: Final = "ttl"
ATTR_UNTIL: Final = "until"
ATTR_RESUME_AFTER_MANUAL: Final = "resume_after_manual"
ATTR_ON_EXPIRE: Final = "on_expire"
ATTR_ONLY_IF_PRESENT: Final = "only_if_present"
ATTR_OWNER: Final = "owner"
ATTR_SOURCE: Final = "source"
ATTR_MANUAL_TIMEOUT: Final = "manual_timeout"
ATTR_STRENGTH: Final = "strength"

# Events
EVENT_RENDER: Final = "layers_render"
EVENT_RENDER_FAILED: Final = "layers_render_failed"
EVENT_EXTERNAL: Final = "layers_external"

# Limits
PRIORITY_MIN: Final = 1
PRIORITY_MAX: Final = 99
LAYER_ID_PATTERN: Final = r"^[a-z0-9_]{1,32}$"
OWNER_MAX_LEN: Final = 64
DECISION_BUFFER: Final = 500          # classifier decisions kept for diagnostics
RENDER_CALL_TIMEOUT_S: Final = 10.0   # one light service call
STALE_UNAVAILABLE_S: Final = 24 * 3600  # status ignores lamps gone longer than this

# Per-lamp service results
RESULT_QUEUED: Final = "queued"
RESULT_UNCHANGED: Final = "unchanged"
RESULT_IN_SYNC: Final = "in_sync"
RESULT_PENDING: Final = "pending"
RESULT_SHADOW: Final = "shadow"
RESULT_SKIPPED_TOMBSTONED: Final = "skipped_tombstoned"
RESULT_SKIPPED_ABSENT: Final = "skipped_absent"
RESULT_SKIPPED_NOT_ENROLLED: Final = "skipped_not_enrolled"

# Status sensor states
STATUS_OK: Final = "ok"
STATUS_PENDING: Final = "pending"
STATUS_FAILED: Final = "failed"
STATUS_SHADOW: Final = "shadow"
