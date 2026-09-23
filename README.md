# Layers

**Let automations borrow a light and give it back, without snapshots.**

You're watching a film, so an automation turns the sofa lamp off. Halfway through, the
washer finishes and another automation turns the same lamp orange to tell you. You empty
the washer. What should the lamp do now?

It should go back to **off**, because the film is still on. When the film ends, it
should go back to **how you had it before**.

Home Assistant doesn't do that on its own. The usual trick is to save a scene, change
the lights, then restore the scene later. That breaks easily: the saved scene is gone
after a restart, the restore undoes whatever someone changed in the meantime, and two
automations borrowing the same lamp restore each other's leftovers.

Layers solves it by stacking. Each automation puts a **layer** on the light and removes
it when it's done. The light always shows the **top layer**, and when that one is removed
it shows the next one down, as it is **now**. Nothing is saved or restored, so nothing
can go stale.

```text
   priority 70   washer    orange        ← the lamp shows the top layer
   priority 40   tv        off
   ───────────── base      on, 60 %      ← how you left it
```

## What you get

- **Automations stop fighting each other.** Higher priority wins; removing a layer
  reveals the one below.
- **People always win.** If someone changes a light by hand (switch, app, voice), Layers
  keeps their setting and gets out of the way on that light.
- **Survives restarts.** Layers and their timers are stored.
- **Makes sure commands arrive.** It checks each light after sending a command, retries
  if needed, and catches up with lights that were offline.
- **A timer never lights a dark room.** When a layer runs out, it may only turn a light
  off or dim it.
- **Opt-in.** Only the lights you choose are managed, and only through Layers' own
  actions. Your other automations and scenes keep working as they are.

## Install

1. In HACS, add `https://github.com/lukab-dev/ha-layers` as a custom repository
   (type *Integration*) and install **Layers**. Or copy `custom_components/layers` into
   your `config/custom_components/` folder.
2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Layers** and pick the lights
   it should manage. Switches (smart plugs, relays) work too, as on/off lights.

Needs Home Assistant 2026.7 or newer.

**Layers starts in watch-only mode.** The **Apply** switch (`switch.layers_apply`) is off,
so it works out what it *would* do but doesn't touch your lights. Try your automations,
check the Activity log, and turn Apply on when you're happy.

## Your first automation

The quickest start is the blueprint, which dims a room while a TV or speaker plays:

[![Import the blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Flukab-dev%2Fha-layers%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flayers%2Fmedia_dim.yaml)

Or write it yourself. Borrowing a light takes one action, and giving it back takes
another:

```yaml
# The film starts: turn the living room lamps off
action: layers.set
target:
  entity_id: [light.floor_lamp, light.sofa_lamp]
data:
  layer: tv          # any name you like
  priority: 40       # 1-99, higher wins
  state: "off"
  transition: 10
```

```yaml
# The film ends: give the lamps back
action: layers.clear
data:
  layer: tv
```

When `tv` is cleared, each lamp goes back to whatever is below it: another layer, or
simply how it was set before.

<details>
<summary><b>Full example: TV dimming plus a washer notification</b></summary>

Two automations that know nothing about each other, sharing the sofa lamp.

```yaml
- alias: "TV: dim the room while playing"
  triggers:
    - trigger: template
      value_template: "{{ is_state('media_player.living_room_tv', 'playing') }}"
      for: "00:00:15"
      id: play
    - trigger: template
      value_template: "{{ not is_state('media_player.living_room_tv', 'playing') }}"
      for: "00:02:00"
      id: stop
  actions:
    - choose:
        - conditions: [{condition: trigger, id: play}]
          sequence:
            - action: layers.set
              target: {entity_id: [light.floor_lamp, light.sofa_lamp]}
              data: {layer: tv, priority: 40, state: "off", transition: 10,
                     ttl: "06:00:00", owner: tv_dim}
            - action: layers.set    # dim the dining lamp only if it is already on
              target: {entity_id: light.dining_pendant}
              data: {layer: tv, priority: 40, mode: adjust, brightness_pct: 25,
                     transition: 10, ttl: "06:00:00", owner: tv_dim}
        - conditions: [{condition: trigger, id: stop}]
          sequence:
            - action: layers.clear
              data: {layer: tv, transition: 3}

- alias: "Washer done: sofa lamp orange"
  triggers:
    - trigger: state
      entity_id: sensor.washer_status
      to: finished
  actions:
    - action: layers.set
      target: {entity_id: light.sofa_lamp}
      data: {layer: washer, priority: 70, state: "on", xy_color: [0.679, 0.318],
             brightness_pct: 100, ttl: "04:00:00", owner: washer_done}

- alias: "Washer emptied: give the lamp back"
  triggers:
    - trigger: state
      entity_id: input_button.washer_emptied
  actions:
    - action: layers.clear
      target: {entity_id: light.sofa_lamp}
      data: {layer: washer, transition: 2}
```

What the sofa lamp does through the evening:

| Time | What happens | Sofa lamp |
|---|---|---|
| 20:00 | you switch it on at 60 % | on, 60 % |
| 20:30 | the film starts | off |
| 21:10 | the washer finishes | orange (the washer layer is above the TV one) |
| 21:15 | you press *emptied* | off again, because the film is still on |
| 22:30 | the film ends | on, 60 % |

</details>

## The basics

**A layer** has a name, a priority from 1 to 99, and what the light should do. Each
light shows its highest-priority layer. A light can hold one layer per priority.

**The base** is what a light shows when it has no layers. You never set it directly: it
is simply how people and your other automations leave the light.

**Two kinds of layer:**
- `set` (the default): decides on or off, plus any brightness or colour you give it.
  Anything you leave out comes from the layer below.
- `mode: adjust`: only changes brightness or colour, and only on a light that is
  already on. It never turns a light on. Use it for "dim whatever is lit to 25 %".

**Timers:** give a layer a `ttl` ("06:00:00") or an `until` (a time) as a safety net in
case the clear never comes. Setting the same layer again restarts the timer. When the
timer runs out, the layer may only turn the light off or dim it. If removing it would
light up or brighten a room, the light stays as it is instead. (`on_expire: render`
turns that rule off.) A `layers.clear` always restores fully.

## When someone changes a light by hand

Layers can tell its own commands apart from everything else. Anything else that changes
a managed light counts as "someone": a person at the switch, the app, voice, a scene, or
another automation.

By default (**Take it back**), their change wins. It becomes the light's new base, and the
layers on that light are removed, on that light only. The automation can't put the same
layer back on that light until it has cleared it. So when the film ends, the lamp you
turned on mid-film stays exactly as you set it.

You can choose a different behaviour in the integration's options, for all lights or
per light:

| Option | When someone changes the light |
|---|---|
| **Take it back** (default) | Their change wins and the layers on that light are removed. |
| **Edit the active layer** | Their change is written into the top layer, which stays. Switching the light off always sticks. |
| **Keep the layers** | Their change becomes the base, the layers stay, and the light keeps showing their change until `layers.sync`. |
| **Reassert** (its own list of lights) | For devices that switch themselves back on, like some smart plugs. A change that comes from the device itself is undone. Changes from the app or automations still win. Don't use it on lights with a wall switch. |

## Actions

| Action | What it does |
|---|---|
| `layers.set` | Put a layer on lights, or update it. |
| `layers.clear` | Remove a layer. Without a target, it's removed from every light. `layer: active` (the top one) and `layer: all` need a target. |
| `layers.sync` | Send every managed light what it should show now, for example after watch-only mode. |
| `layers.get` | Returns each light's base, layers and current command, for debugging. |

`layers.set` options: `layer`, `priority` (needed the first time a layer goes on a
light), `mode`, `state`, `brightness` / `brightness_pct`, `color_temp_kelvin`,
`xy_color` / `hs_color` / `rgb_color`, `transition`, `ttl` / `until`, `on_expire`,
`resume_after_manual`, `only_if_present` and `owner`. The action's UI form describes each
one.

## Entities

- **`switch.layers_apply`**: off means watch-only. Layers still keeps track of
  everything but sends nothing.
- **`sensor.layers_status`**: `ok`, `pending` (still delivering), `failed` or `shadow`
  (watch-only).

The Activity log shows which layer changed a light, and on whose behalf.

## Details

<details>
<summary><b>What counts as "someone changed it", and what doesn't</b></summary>

Layers identifies its own commands by their context ids. These are **not** treated as a
change:

- a light dropping off the network and coming back;
- a report that has already gone back to its previous value, such as a sub-second blip;
- a bridge undoing Layers' own command shortly after accepting it. That's treated as a
  failed delivery and retried once. (Someone else's command is never re-sent: an Off
  that got undone is repaired on the next `layers.*` call to that light; an On that got
  undone counts as a change.)

A change that turns a light on keeps everything the light then shows, even if the
command only said "on" (Apple Home, Assist). The base learns the brightness and colour
it came on at, so a later clear can restore them. If a person changes a light while
Layers is still delivering to it, the delivery is cancelled and the person's change wins.

`resume_after_manual` on a layer lets it come back to a light once that light has been
seen off. That suits a nightlight: if the "all off" catches it mid-walk, the next motion
can light it again.

</details>

<details>
<summary><b>When Layers sends a command</b></summary>

Only when:

- a `layers.set` or `layers.clear` changed what a light should show;
- you call `layers.sync`;
- a timer ran out and the rule above allows the change;
- a light comes back online and is still owed a command, provided nobody touched it
  while it was away;
- something replayed an old command to a light (a retry loop re-sending a button press),
  and the layers are put back;
- a retry of any of these.

Never because someone else changed a light, never just because Home Assistant started,
and never while **Apply** is off. A light nobody layers is never commanded.

</details>

<details>
<summary><b>Response values and other notes</b></summary>

- `set`, `clear` and `sync` can return a result per light: `queued`, `unchanged`,
  `in_sync`, `pending`, `shadow`, `skipped_tombstoned` (someone took the light back),
  `skipped_absent` or `skipped_not_enrolled`.
- Light groups in a target are expanded to their members. In the settings, pick
  individual lights, not groups.
- Colours are sent exactly as you write them: `xy_color` stays xy, because some lamps
  wash out hs or rgb colours.
- The idea comes from building automation, where BACnet has used priority arrays since
  1995.

</details>

## Development

```bash
python -m pytest tests/logic                  # decision logic, any Python >= 3.12
uv venv --python 3.14 .venv-ha && uv pip install --python .venv-ha/bin/python -r requirements_test.txt
.venv-ha/bin/python -m pytest tests/ha        # the integration inside Home Assistant
```

The decisions live in `custom_components/layers/logic/`, which has no Home Assistant
imports, so it can be tested directly and replayed against recorded history. The full
specification is [docs/SPEC.md](docs/SPEC.md).

## Licence

MIT.
