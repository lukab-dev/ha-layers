# Roadmap

What is known to be missing or wrong, and what might come next. Nothing here has a date.
If you want something on this list, or something that isn't, open an issue.

## Known issues

None right now.

## Next

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
- **Covers**: position and tilt. Locked layers would fit wind and rain protection.
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
