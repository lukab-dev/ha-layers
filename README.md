# Layers

Priority layers for Home Assistant lights. An automation *borrows* a lamp by putting a
layer on it, and gives it back by clearing the layer. The lamp then shows whatever is
below that layer **now**, not a snapshot taken earlier.

- **No snapshots, so nothing goes stale.** Layers hold live commands. Clearing one falls
  through to the current state of the layer below.
- **Survives restarts.** Layers are stored, and time limits are absolute times.
- **Never fights a person.** When something other than Layers changes a lamp, a per-lamp
  policy decides what that means. By default the change becomes the lamp's new base, and
  the layers above it are dropped on that lamp only.
- **Checks that commands arrived.** Home Assistant silently skips unavailable lamps, and
  vendor bridges sometimes acknowledge a command and then revert it. Layers reads each lamp
  back after commanding it, retries with backoff, and owes the command to a lamp that is
  offline until it returns.
- **Opt-in and small.** It only touches the lamps you enrol, and only through its own
  services. It does not intercept Home Assistant's light services, and it uses only
  documented APIs.

## Why

The usual way to borrow a lamp is `scene.create` → change it → `scene.turn_on` later.
That breaks in every way it can:

- The snapshot is lost on restart and on `scene.reload`.
- The restore undoes whatever a person did in the meantime.
- A restore with no snapshot lights a dark room.
- A second trigger snapshots the automation's own output.

Layers is the priority-array idea from building automation (BACnet has had it since
1995), adapted for a house where people also use the switches.

## How it works

Each enrolled lamp has:

- a **base**: what it shows when nothing is layered on it. The base follows whatever
  people and other automations do to the lamp.
- a stack of **layers**, each with an id, a priority (1–99, higher on top) and a command.

A layer's mode is one of:

- **`set`**: decides on/off and its own brightness and colour. Attributes it doesn't
  give come from below.
- **`adjust`**: only changes brightness or colour, and only on a lamp that is already on.
  It never turns a lamp on. Use it for "dim whatever is lit to 25 %".

Colours are passed through exactly as written: an `xy_color` stays xy, because some lamps
desaturate hs or rgb. Each lamp can hold one layer per priority.

### When someone else changes a lamp

Layers tells its own commands apart from everyone else's by their context ids. Every
other change counts as external, whoever made it: a person in the app, a wall switch, a
scene, or another automation. What an external change means depends on the lamp's policy:

| Policy | The change | The layers |
|---|---|---|
| `take_back` (default) | becomes the lamp's base | are dropped on that lamp, and each id is blocked there until its owner clears it |
| `edit_active` | is written into the layer on top; switching the lamp off (or on, from off) also becomes its base | stay; the edit is lost when that layer is cleared, but a switch-off never is |
| `base_keep_layers` | becomes the base, but the lamp keeps showing your change | stay; `layers.sync` re-applies them |

A layer set with `resume_after_manual` is let back onto a lamp once that lamp has been
seen off. That suits a nightlight: if the house-wide off catches it mid-walk, the next
motion can relight it.

A change that turns a lamp on takes everything the lamp then shows, even when the
call named only the state (Apple Home's "on", Assist's "turn on X"): the base learns
the brightness and colour the lamp came on at, so a later `clear` can restore them.
A person changing a lamp while Layers is still delivering a command to it is not
fought either: the delivery is cancelled and their change is taken.

Some changes are not treated as external:

- a lamp dropping off the network and coming back;
- a report that has already reverted to its previous value, such as a sub-second blip;
- a bridge reverting Layers' own command within its reversal window: a failed delivery,
  retried once. (Someone else's command is never re-sent: a reverted Off is marked for
  the next `layers.*` call on the lamp to repair; a reverted On counts as a change.)

### Leases and expiry

A layer can have a `ttl` or an `until`. Owners can renew a `ttl` by setting the same
layer again: an identical request only extends the time (one without a `ttl` leaves
the lease as it is). That makes the `ttl` a lease that lapses if its owner stops
renewing it.

**When a layer expires, it may only turn a lamp off or dim it.** If expiring would turn
a lamp on or brighten it, the layer is dropped and the lamp stays as it is. Not knowing
how a room should be lit is never a reason to light it. Set `on_expire: render` to opt
out. An explicit `layers.clear` always restores fully.

### When Layers sends a command

Only in these cases:

- a `layers.set` or `layers.clear` changed the lamp's effective command;
- `layers.sync`;
- an expiry allowed by the rule above;
- a lamp coming back to a command it is owed, provided nobody touched it while it was away;
- the layers going back on after a retry loop re-sent an old button press;
- a retry of any of these.

Never because someone else changed a lamp, never just because Home Assistant started,
and never while the **Apply** switch is off. A lamp left out of step on purpose (by
observe-only mode, or a manual change kept by `base_keep_layers`) is only pushed by
`layers.sync`. A lamp nobody layers is never commanded.

## Services

`layers.set`:

```yaml
action: layers.set
target:
  entity_id: [light.lamp_a, light.lamp_b]   # light groups are expanded
data:
  layer: tv            # an id, or "base", or "active" (whatever is on top)
  priority: 40         # needed when the layer is new on a lamp
  state: "off"         # or "on" with brightness / colour
  transition: 10
  ttl: "06:00:00"      # or until: "{{ today_at('23:00') }}"
  owner: tv_dim
```

```yaml
action: layers.set          # dim what is lit, leave what is off alone
target: {entity_id: [light.lamp_c, light.lamp_d]}
data: {layer: tv, priority: 40, mode: adjust, brightness_pct: 25}
```

`layers.clear`: remove a layer. Its lamps fall back to whatever is below it now.

```yaml
action: layers.clear
data: {layer: tv, transition: 3}    # without a target: wherever the layer is
```

`layer: active` (the layer on top) and `layer: all` need a target.

- `layers.sync`: push the model onto lamps, for example after observe-only mode.
- `layers.get`: returns each lamp's base, layers, effective command and status.

`set`, `clear` and `sync` return a result per lamp when you ask for a response:
`queued`, `unchanged`, `in_sync`, `pending`, `shadow`, `skipped_tombstoned`,
`skipped_absent` or `skipped_not_enrolled`.

## Entities

- **`switch.layers_apply`**: off means observe only. Layers are still set and changes
  still classified, but nothing is sent. It starts **off** on a fresh install.
- **`sensor.layers_status`**: `ok`, `pending`, `failed` or `shadow`.

The Activity log shows which layer changed a lamp, and on whose behalf.

## Install

Through HACS: add this repository as a custom repository (category *Integration*),
install **Layers**, restart Home Assistant, then add the integration under Settings →
Devices & services.

Manually: copy `custom_components/layers` into your `config/custom_components/` and
restart.

Configuration is entirely in the UI:

- the lamps to manage (individual lamps only: groups are expanded by Layers itself);
- the default policy for manual changes;
- optionally, lamps with a different policy.

Start with **Apply** off, watch the decisions in the diagnostics download for a few
days, then turn it on.

## Development

```bash
python -m pytest tests/logic                  # pure decision logic, any Python >= 3.12
uv venv --python 3.14 .venv-ha && uv pip install --python .venv-ha/bin/python -r requirements_test.txt
.venv-ha/bin/python -m pytest tests/ha        # the integration inside Home Assistant
```

The decisions live in `custom_components/layers/logic/`, which has no Home Assistant
imports. That lets it be tested directly and replayed against recorded history. The
specification is [docs/SPEC.md](docs/SPEC.md).

## Licence

MIT.
