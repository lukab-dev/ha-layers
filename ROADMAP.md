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

## Thermostats: open questions

Climate entities are managed since this pre-release. What is still open:

- **Two setpoints.** Thermostats in `heat_cool` with `target_temp_low`/`target_temp_high`
  only get their mode and preset; a range layer needs its own answer for `adjust`.
- **Fan, swing, humidity** are not layered.
- **A thermostat that changes itself** (the vendor's open-window detection, its own
  schedule) looks like a person and gets a manual layer. Telling those apart would need
  a per-device source, like the lamps' **reassert** list.
- **The manual layer is always on.** A thermostat has no "take it back" or "keep the
  layers" choice yet, and one manual priority and timeout cover every thermostat.
- **A person inside the report-back window.** Someone turning the dial in the first
  seconds after Layers' own command, before the thermostat has shown it, is sent over
  once (the same price lamps pay, for the same reason).
- **An expiry that ends a manual "off".** The safety rule keeps the heating off when a
  person's off runs out; only the next `layers.*` call, or `layers.sync`, turns it back
  on. Whether a person's own hold should be allowed to end in heating is open.

## Later: more domains

The same model fits other things that several automations fight over:

- **Covers**: position and tilt. Locked layers would fit wind and rain protection.
- **Fans**: on/off, speed, preset.

Each new domain needs its own answers before it can be built, as climate did:

- **What a layer holds**, and what `adjust` means for it.
- **The expiry safety rule.** For lights, a timer running out never lights a dark room;
  for thermostats, it never switches the heating on. For covers, never open a blind?
- **Telling a person from a slow device.** Covers take a long time to move. A late report
  must not look like someone changing it by hand.
- **Checking a command arrived**, when the device reports slowly or rounds values.

Tell us in an issue which domain you'd use.
