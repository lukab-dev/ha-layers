# Roadmap

What is known to be missing or wrong, and what might come next. Nothing here has a date.
If you want something on this list, or something that isn't, open an issue.

## Known issues

- **A scene skips lights that already match it, so Layers never hears about them.**
  Home Assistant sends no command to a scene member that already looks the way the scene
  wants. Example: a layer holds the ceiling light off, and you run a bedtime scene that
  also turns it off. Home Assistant skips the ceiling, so Layers doesn't learn that "off"
  is what you want now. When the layer is cleared later, the light goes back to the old
  base and turns on. Planned fix: when a scene runs, a managed member that got no command
  within a few seconds is treated as if the scene had set it to what it already shows.

## Next

- **A `follow` layer for Adaptive Lighting and circadian lighting.** Today Adaptive
  Lighting can't control managed lights directly (Layers reads every change it didn't make
  as "someone changed the light"), and feeding its values into an `adjust` layer breaks
  when someone switches a light on by hand. The plan:
  - `layers.set mode: follow, source: <entity>`: the layer follows the brightness and
    colour attributes of an entity, such as Adaptive Lighting's switch or a template
    sensor. Layers does no circadian maths itself.
  - It never turns a light on. Switching a light on or off by hand doesn't remove it.
  - A hand change takes over only what it changed: dim a light by hand and its brightness
    stops following, but its colour keeps adapting. Control comes back when the light goes
    off, or after an optional timeout.
  - A light switched on by hand gets the follow values right away.
- **Protected layers.** Two levels:
  - `protected: automations`: only the owner's `clear` or a person removes it. Other
    automations, scenes and `clear all` don't.
  - `protected: full`: only the owner's `clear` removes it. A person's change is undone
    after a few seconds. It needs a `ttl` or `until`, and the Apply switch still stops it.
    Meant for real alarms (leak, smoke).
- **A sensor per light**, showing which layer is on top and the whole stack in its
  attributes, for dashboard cards and for automations that need to check a light.
- **Area and label targets** on `layers.*`. The code accepts them already; they need tests
  and documentation.
- **Manual-change behaviour by source**: choose what happens separately for a wall switch,
  the app, voice and other automations.
- **A "never brighten" cap for `adjust` layers**, so a dim layer can't make a light
  brighter than it already is.

## Later: more than lights and switches

Layers manages `light` and `switch` entities today. The same model fits other things
that several automations fight over:

- **Climate**: target temperature, HVAC mode, preset. An `adjust` layer would be an
  offset ("2 °C lower while nobody is home").
- **Covers**: position and tilt. Protected layers would fit wind and rain protection.
- **Fans**: on/off, speed, preset.

Each new domain needs its own answers before it can be built:

- **What a layer holds**, and what `adjust` means for it.
- **The expiry safety rule.** For lights, a timer running out never lights a dark room.
  For heating, should an expiry never start heating? For covers, never open a blind?
- **Telling a person from a slow device.** Thermostats and cloud integrations can report
  back many seconds late, and covers take a long time to move. A late report must not look
  like someone changing it by hand.
- **Checking a command arrived**, when the device reports slowly or rounds values (a
  setpoint of 21.3 °C that reads back as 21.5 °C).

Climate is likely first. Tell us in an issue which domain you'd use.
