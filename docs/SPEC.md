# Layers — specification

This is the contract every module implements. Examples use made-up entity ids
(`light.lamp_a`, `light.lamp_b`). Types referenced here are defined in
`custom_components/layers/logic/model.py`; constants (tolerances, windows) are
the `*_S` / `TOL_*` names there.

## 1. The idea

Each enrolled lamp has a **base** and a stack of named **layers**. A layer holds
a *live command*, not a snapshot: removing it makes the lamp fall through to
whatever the next layer, or the base, says **now**. Nothing is ever captured and
restored, so nothing can go stale, be lost on restart, or be restored wrongly.

People and other automations keep using lights normally. When something other
than Layers changes an enrolled lamp, a per-lamp **policy** decides what that
means for the stack. The default, `take_back`, makes the change the new base and
drops the layers above it on that lamp only. Layers never fights a person.

## 2. Model

See `model.py`. One `Record` per enrolled lamp:

- `base`: a `Command`, or `None` when unknown. Normally a full command; a
  change that took only some groups on an unknown base (e.g. brightness alone)
  leaves attributes with no state, which resolves like an unknown base (`None`,
  "do nothing") until something gives it a state. Only persisted once the lamp
  has a layer, tombstone, owed render or divergence (`Record.worth_persisting`).
  A `layers.set` on a lamp that has no layer and no known base (the startup or
  reload grace) first records what the lamp shows as its base
  (`base_source = "observed"`), so releasing the layer restores it instead of
  leaving the layer's output behind.
- `layers[id]`: `Layer(priority, mode, requested, command, seq, set_at,
  expires_at, owner, resume_after_manual, on_expire, requested_mode)`.
  - `requested` is what the owner last sent. `command` is `requested` plus any
    edit made through `layer: active` or the `edit_active` policy.
  - `requested_mode` is `None` except while such an edit has changed `mode`
    (an `adjust` layer turned off becomes `set`/off): it then holds the mode
    the owner asked for, so the owner's unchanged request still only refreshes.
  - **One layer id per priority per lamp.** Setting a new id at a priority
    another id already holds on that lamp is an error (`priority_conflict`).
- `tombstones[id]`: created when `take_back` drops a layer. A later `set` of that
  id on that lamp is skipped until the owner `clear`s the id or the tombstone
  expires (it inherits the dropped layer's `expires_at`). With
  `lift_when_off` (from `resume_after_manual`) it also lifts the next time the
  lamp is observed `off`.
- `observed` (latest report), `observed_prev` (the report before the current
  debounce began), `p_at_drop` (what the lamp showed when it went away: frozen
  when it went unavailable or its entity was removed, never overwritten on
  return). The engine also seeds it when the Store loads, from the persisted
  `observed` of every lamp (Home Assistant's downtime counts as time away), and
  clears it after every `decide_return`, whenever a change on an available lamp
  supersedes a pending return, and, for available lamps, at the end of the
  startup grace, so it never describes an older absence.
- `owed`: a command Layers owes the lamp — a render that started and was not yet
  verified (persisted at start, cleared on verification), or a foreign command
  aimed at the lamp while it was unavailable (`missed=True`).
- `diverged`: why the lamp does not show its effective command. `delivery`,
  `partial` and `colour` are repaired by the next `layers.set`/`clear` that
  targets the lamp; `manual_keep` and `unsynced` only by `layers.sync`: no
  deferred render (7.3 c-f) pushes them. A `layers.set`/`clear` that changes the
  lamp's effective command sends it and clears them. A lamp found showing its
  effective command has no divergence.
- `last_command`: the last command sent to the lamp by anyone (for the late
  window), with `from_state`, the lamp's on/off when it was sent. A foreign
  command known not to turn the lamp on or off does not replace a command that
  did, inside that command's late window: only a flip can be reverted by a flip.
- `last_external` (with the attribute `groups` the change took),
  `last_layers_change` (time of the last change to the lamp's layers, for the
  replay rule).
- `untrusted`: an exception was raised while handling the lamp. Deferred renders
  and its TTL skip it; its render is cancelled; `layers.sync` clears it. Shown in
  `layers.get`, diagnostics and the status sensor's `untrusted` attribute.

## 3. Resolver (`logic/resolve.py`)

```
live_layers(rec, now) -> list[Layer]     # expires_at is None or > now; sorted by (priority, seq)
resolve(rec, now) -> Resolution          # (command, active)
active_layer(rec, now) -> Layer | None   # the layer resolve() names as active, if any
state_holder(rec, now) -> Layer | None   # the top live `set` layer: the one deciding on/off
```

Fold, bottom to top:

1. Start with `state, brightness, color` from `base` (all `None` if base is
   unknown). `active = "base"` if base has a state, else `"none"`.
2. For each live layer in ascending `(priority, seq)`:
   - `set` + command off → `state = off` (brightness/colour below are kept in
     the fold so a higher `set on` without attributes inherits them).
     `active = layer.id`.
   - `set` + command on, or with no state (a `set` command without a state
     counts as on) → `state = on`; its brightness and colour replace those
     below when given. `active = layer.id`.
   - `adjust` → only if `state == on`: its brightness/colour replace those below
     when given; `active = layer.id`. Over an off result it does nothing and does
     not become active.
3. `state is None` → `Resolution(None, active)` ("do nothing").
   `state == off` → `Command(off)`. Otherwise `Command(on, brightness, color)`.

An active `adjust` layer does not hold the lamp: its "on" comes from below.
`state_holder()` is what the expiry rule (5.4) and the return rule (6.4) ask
when they need to know whether an owner still holds the lamp, so neither ever
lights a lamp through an adjust layer.

Colour is one atomic group (`xy_color` | `hs_color` | `rgb_color` |
`color_temp_kelvin`): a layer's colour replaces the one below entirely, never
merges. Colours are carried exactly as written.

## 4. Capabilities (`logic/capability.py`)

```
caps_from_attrs(attrs: Mapping, platform: str) -> Caps
observed_from_state(state: str, attrs: Mapping, at: float) -> Observed
observed_to_command(obs: Observed, caps: Caps) -> Command | None
project(cmd: Command | None, caps: Caps) -> Call | None
matches(obs: Observed, call: Call, caps: Caps) -> "yes" | "no" | "colour_off"
compared_groups(obs: Observed, call: Call, caps: Caps) -> frozenset[str]
close(a: Observed, b: Observed) -> bool
raises_output(before: Observed | Command | None, after: Command) -> bool
kelvin_to_xy(kelvin: float) -> tuple[float, float]
```

- `caps_from_attrs`: `supported_color_modes` → `modes`; `min_color_temp_kelvin`,
  `max_color_temp_kelvin`; `transition` from `supported_features & 32`.
  A `switch.*` entity has none of these, so its `Caps` are empty: `project` sends a
  bare `turn_on` / `turn_off`, `matches` compares its state alone, and `raises_output`
  is true only for off → on. Enrolment, target expansion, the call listener and the
  render call take `light.*` and `switch.*` alike (`const.MANAGED_DOMAINS`); a call in one
  domain is only ever read against enrolled entities of that domain.
- `observed_from_state`: `brightness`, `color_mode`, `xy_color`, `hs_color`,
  `color_temp_kelvin` from the attributes (ints/tuples; missing → `None`).
- `observed_to_command` (used when recording what a lamp shows as a command):
  unavailable/unknown → `None`; `off` → `Command(off)`; `on` → brightness plus
  colour by `color_mode`: `color_temp` → kelvin; `xy`, `hs`, `rgb`, `rgbw`,
  `rgbww` → `xy_color` from the reported xy (fall back to `hs_color`);
  `brightness`, `onoff`, `white` → no colour.
- `project` turns an effective command into the call a lamp can take:
  - `None` → `None`. Off → `turn_off` with no data.
  - On → `turn_on` with `brightness` (clamped 1–255) if the lamp has any
    brightness-capable mode (anything but `onoff`); colour:
    - kelvin: if the lamp supports `color_temp`, clamp to its min/max and send;
      else if it supports a colour mode, send kelvin as is (HA emulates it);
      else drop it.
    - xy/hs/rgb: send as written if the lamp supports a colour mode
      (`hs`, `xy`, `rgb`, `rgbw`, `rgbww`); else drop it.
  - Transition is added by the renderer, only if `caps.transition`.
- `matches` (verification):
  - state must match the call (`turn_off` ↔ `off`, `turn_on` ↔ `on`);
    a `turn_off` is `yes` on state alone.
  - brightness within `TOL_BRIGHTNESS` when the call has it and the lamp reports it.
  - kelvin requested:
    - reported `color_mode == color_temp`: the reported kelvin within `TOL_KELVIN`;
    - a lamp that has a `color_temp` mode but reports another did not take it;
    - a lamp without a `color_temp` mode: Home Assistant emulated the kelvin
      (through hs), so the reported xy must be within `TOL_XY` of
      `kelvin_to_xy(kelvin)`; an `rgbww` lamp gets white channels instead,
      whose xy cannot be predicted: not compared.
    Otherwise `colour_off`.
  - xy requested: reported xy within `TOL_XY` on both axes; otherwise
    `colour_off`. hs/rgb requested: colour not compared.
  - state or brightness wrong → `no`.
- `kelvin_to_xy(k)`: the xy Home Assistant gives a colour temperature, exactly
  as `homeassistant.util.color` computes it (`color_hs_to_xy(*color_temperature_to_hs(k))`:
  T. Helland's RGB, through hs, to the Wide RGB D65 xy, rounded to 3 decimals).
  Home Assistant uses it both to emulate kelvin on colour lamps and for a CT
  lamp's reported `xy_color`. It is not the Planckian locus (2700 K comes out
  near (0.525, 0.388), not (0.460, 0.411)).
- `compared_groups`: the attribute groups `matches` actually checks for a
  report and a `turn_on` call: `brightness` when the call has it and the lamp
  reports one; `color` for xy, and for kelvin unless it is not compared (above).
  Empty when the lamp is not on.
- `close(a, b)`: same state; brightness within `TOL_BRIGHTNESS` (or either
  missing); same colour within tolerance when both have it.
- `raises_output(before, after)`: `True` if `after` turns the lamp on when
  `before` is off/unknown, or raises brightness by more than `TOL_BRIGHTNESS`.
  Used by the expiry safety rule.

## 5. Policy (`logic/policy.py`)

All functions mutate the `Record` in place and return a small result object.
None of them sends anything; the caller compares `resolve()` before and after
(and, for the few things that comparison cannot see, reads the result: see
5.2 and 5.4).

### 5.1 `apply_set(rec, req: SetRequest, now, next_seq: Callable[[], int]) -> SetResult`

`SetResult.result` is one of `created`, `updated`, `refreshed`, `base_set`,
`active_set`, `skipped_tombstoned`, `skipped_absent`; errors raise
`PolicyError(code)` before anything changes, with code `priority_required`,
`priority_conflict` or `invalid_request` (a command that sets no state,
brightness or colour; a named layer whose `expires_at` is not after `now`; the
id `all`; an unknown mode).

- `layer: base` → `base = merge_command(base, req.command)`; if base was
  unknown and the command has no state, the state is `on`. `base_source =
  "service"`. Result `base_set`.
- `layer: active` → the top layer from `active_layer()`; none → handled as
  `base`. On an `adjust` top, a request with `state: off` converts the layer to
  `set`/off (keeping `requested_mode = adjust`); otherwise attributes merge
  into `command` (not `requested`). On a `set` top, `command =
  merge_command(command, req.command)`. Result `active_set`.
- named id:
  - tombstoned (live tombstone for that id) → `skipped_tombstoned`.
  - absent (or expired, not yet removed): `only_if_present` → `skipped_absent`;
    no `priority` → `priority_required`; priority held by another id →
    `priority_conflict`; otherwise create with `requested = command =
    req.command`, `seq = next_seq()`. Result `created`.
  - The request's mode is `req.mode`, except with `only_if_present`: a renewal
    never changes a layer's mode (`SetRequest.mode` defaults to `set`, and a
    renewal that leaves it out must not turn an adjust layer into a set layer
    that lights the lamp). Its command is read in the layer's mode: on a `set`
    layer a command without a state means "on like this" (as at creation), on an
    `adjust` layer a `state: on` is dropped. The service leaves a renewal's
    state out unless it is given, so a renewal that repeats the attributes only
    refreshes either kind of layer.
  - present and `req.command == layer.requested` and the request's mode is the
    mode the owner asked for (`requested_mode`, else `mode`) and priority
    unchanged or omitted → refresh only: update `expires_at` when the request
    gives one (a renewal without `ttl`/`until` keeps the lease the layer has: an
    identical set only extends the time, it never silently makes a leased layer
    permanent), and `owner` when the request gives one. Nothing else: options a
    renewal leaves out keep their values. Result `refreshed`. **No render, no
    re-stacking, edits to `command` survive** (including an adjust layer turned
    set/off).
  - present and different → `requested = command = req.command`, `mode` = the
    request's mode, `requested_mode = None`, update priority (conflict check),
    expiry, `owner` when given, `resume_after_manual` and `on_expire`; keep
    `seq`. Result `updated`.
- Every result except `refreshed`, `skipped_*` sets `last_layers_change = now`.

### 5.2 `apply_clear(rec, layer, now) -> ClearResult`

- id → remove that layer and that id's tombstone.
- `active` → remove the top layer (if any).
- `all` → remove every layer and tombstone.
- A layer whose `expires_at` has passed but that `expire()` has not removed yet
  (Home Assistant was down, or the startup grace holds its timer) is not
  removed: it is marked `on_expire = render` and left for `expire()`, which
  removes it and renders like this clear. An owner's clear is a restore, and the
  safety rule must not swallow it; removing the layer here instead would let
  the startup record what it showed as the base. `resolve()` before and after
  cannot see this; the render happens when `expire()` runs (the end of the
  grace, or the next TTL tick).
- `ClearResult(removed, lifted, expired)`: layer ids removed, tombstone ids
  lifted, expired layer ids marked. Sets `last_layers_change` if a layer was
  removed or marked; lifting a tombstone alone changes no layer and does not.

### 5.3 `apply_external(rec, shown: Command, groups, source, policy, now, user_id=None, *, caps=None) -> ExternalResult`

A change Layers did not make. `shown` is the command the lamp now shows (or the
intent from a known service call); `groups` is `None` (take every group
`shown` specifies) or the attribute groups a known call specified. What the
lamp shows is read from `rec.observed`, which the caller updates first; `caps`
are the lamp's (without them the reported colour mode stands in for its modes).

"The lamp shows C" below means `matches(rec.observed, project(C, caps)) == yes`;
it is unknown when the report is missing or unavailable.

- `take_back`:
  - `base = merge_command(base, shown, groups)`; if base was unknown, the
    merge starts from nothing.
  - every layer on the lamp is dropped and tombstoned (tombstone
    `expires_at = layer.expires_at`, `lift_when_off = layer.resume_after_manual`).
  - **partial**: `groups` is given, the new effective command is on, and the
    lamp does not show its *rest*: its brightness and colour in the groups the
    change did not take (e.g. only brightness was set while a layer's colour
    was showing; on the intent path, a call naming only the brightness the lamp
    already had). Then `diverged = partial`; otherwise `diverged = None`. A
    lamp that shows the rest anyway, or whose report is unknown, is not partial.
- `edit_active`: top layer (via `active_layer`) → `command =
  merge_command(command, shown, groups)`; an `adjust` top with `shown` off (and
  `state` among the groups taken) becomes `set`/off with `requested_mode =
  adjust`. `requested` is untouched. An on/off decision also reaches the base, so
  a later clear never undoes it: with `state` among the groups taken, an off
  makes the base off, and an on over a base that is off or unknown is merged
  into it (an on over a base that is on leaves the base alone). `diverged` is
  left as it is. No top layer → like `take_back`.
- `base_keep_layers`: `base = merge_command(base, shown, groups)`; the layers
  stay. If a layer is active and the lamp does not show the new effective
  command, `diverged = manual_keep` (no snap-back: the lamp keeps showing the
  person's change until `layers.sync`). Otherwise `diverged` follows the
  `take_back` partial rule.
- `reassert` (per-entity only, never the default): a `device` change (no service
  call behind it) while a layer is active is the device misbehaving, not a
  person. The base and the layers stay, `diverged = delivery`, and the result's
  `reassert` is true: the engine re-renders the effective command (7.3 g). A
  `user` or `automation` change, a change with no active layer, or a second
  `device` change within `REASSERT_COOLDOWN_S` (30 s) of a reassert — someone is
  at the device's own button — is a `take_back` (recorded as such in
  `last_external.policy`, so the cooldown is measured from the last reassert).
- `replay` (`POLICY_REPLAY`) → `apply_replay` below, so a `FOLLOW_UP` can
  re-apply `last_external.policy` as it is.
- `take_back`, `edit_active`, `base_keep_layers`, `reassert`: `owed = None`. All
  policies: `last_external = External(..., groups)`. None touches `last_layers_change`.
- `ExternalResult(dropped: tuple[str, ...], edited: str | None, partial: bool,
  reassert: bool)`.

`apply_replay(rec, shown, groups, source, now, user_id=None) -> ExternalResult`:
a foreign press that the replay rule (6.1) caught. `base = merge_command(base,
shown, groups)` (`base_source = source`); the layers, tombstones, `diverged`
and `owed` stay as they are; `last_external = External(policy="replay")`. The
engine re-renders `REPLAY_QUIET_S` after the last replayed call (7.3 f).

### 5.4 `expire(rec, now, observed, caps) -> ExpireResult`

Removes layers and tombstones whose `expires_at <= now`. Then:

- If nothing expired → `ExpireResult(expired=(), render=False)`.
- If a live `set` layer still decides on/off after removal (`state_holder()`)
  → `render=True` (an owner still holds the lamp). An active `adjust` layer
  does not count: its "on" is the base's.
- Else: if any expired layer had `on_expire=render` (which includes one an
  owner cleared after it ran out, 5.2) → `render=True`. Otherwise the
  **safety rule**: compare what the lamp shows (`observed`, or `p_at_drop` if
  unavailable) with the new effective command; if `raises_output(shown,
  effective)` → **do not render**: `base = observed_to_command(shown)`,
  `base_source = "expiry"`; `render=False`. Otherwise `render=True`.
- `render` is never true when the new effective command is `None`.
- If the lamp is unavailable and a render is allowed, the caller records it as
  `owed`.

`last_layers_change = now` when a layer expired (tombstones running out on
their own do not set it).

### 5.5 Small helpers

- `lift_on_off(rec)`: remove tombstones with `lift_when_off` (called when the
  lamp is observed off).
- `record_observed_as_base(rec, obs, caps, now, source)`: `base =
  observed_to_command(obs, caps)`.

## 6. Classifier (`logic/classify.py`)

Pure functions over a `Record`, the incoming event, and a `Runtime` snapshot the
engine passes in (nothing here keeps state).

```python
@dataclass(frozen=True)
class StateEvent:
    entity_id: str
    old: Observed | None
    new: Observed | None
    context_id: str | None
    parent_id: str | None
    user_id: str | None
    at: float

@dataclass(frozen=True)
class CallInfo:                 # a foreign light.* service call, normalised
    context_id: str
    source: str                 # "user" if user_id else "automation"
    user_id: str | None
    service: str                # "turn_on" | "turn_off" | "toggle"
    command: Command | None     # the intent (None for toggle, or an intent it cannot know)
    groups: frozenset[str]      # attribute groups the call specified
    lamps: frozenset[str]       # enrolled lamps it targets (groups/rooms expanded)
    via_room: frozenset[str]    # the subset reached through a vendor room/zone group
    first_seen: float           # when this context id was FIRST seen (kept across re-sends)

@dataclass(frozen=True)
class OurCommand:
    context_id: str
    target: Command
    at: float

@dataclass(frozen=True)
class Runtime:
    ours: Mapping[str, OurCommand]          # our command per context id, for this lamp (see 7.2)
    render_alive: bool                      # a render task of ours is running on this lamp
    calls: Mapping[str, CallInfo]           # foreign calls, keyed by context id (<= CALL_MEMORY_S old)
    room_call: CallInfo | None              # a recent room-group call reaching this lamp, if any
    returning: bool = False                 # the lamp came back; decide_return has not judged it yet
```

`first_seen` is when the engine first saw that context id, not when this copy
of the call arrived: a retry loop re-sends the same context id at +1, +3, +7,
+15 and +31 s, and the replay rule needs the first time. The engine remembers
first sightings for at least 64 s (the 1 + 2 + 4 + 8 + 16 + 32 s of such a
loop), longer than a call stays in the `CALL_MEMORY_S` attribution map.

### 6.1 `classify_state(rec, ev, rt, caps, now) -> Verdict`

`Verdict(kind, source=None, call=None, groups=None, replay=False, ours=None,
command=None)`, kinds in the order they are tested:

1. `GONE` — `ev.new is None`.
2. `TRANSPORT_DOWN` — new state unavailable/unknown (and old was not).
   `IGNORE` if both are unavailable/unknown.
3. `FIRST` — `ev.old is None` (the entity just appeared, e.g. at startup):
   update `observed` only.
4. `TRANSPORT_UP` — old unavailable/unknown, new available. The engine judges
   the return after `RETURN_SETTLE_S` with `decide_return`, and passes
   `rt.returning = True` until then.
5. `OURS` — `ev.context_id` is in `rt.ours` **and** the new state is
   consistent with that command: `matches(new, project(target))` is not `no`,
   or it is a step towards it during a transition — the target's on/off state,
   and no further from the target than the report before (brightness, kelvin
   and xy compared where the target and both reports carry them, within
   tolerance). A dimming step while our target is off, or a person turning the
   lamp up, is not a step towards it.
   A write carrying a context of ours with an inconsistent state is someone
   else's inside Home Assistant's 5-second context reuse: it is judged as
   having no context, except that it is never a late-window `FAILED_DELIVERY`
   (8.3): the measured bridge reversals arrive 9.7–38 s after a command,
   without context. "A context of ours" is one in `rt.ours`, or the context of
   `rec.last_command` when that command was ours (it may already have left
   `rt.ours`).
6. `EXTERNAL` via a known call — `ev.context_id in rt.calls` →
   `source = call.source`, `call`, `groups`: the call's groups plus `state` for
   a `turn_on`/`turn_off` (which always decides the state); `None` (every group
   the lamp shows) for a toggle, an intent it cannot know (`call.command is
   None`: brightness steps, profiles), a call that does not name this lamp, or
   a report that flips the lamp on/off: a bare `turn_on` (Apple Home, Assist)
   names only the state, but the lamp coming on decided its brightness and
   colour too, as the device path treats a flip (6.2). Taking `{state}` alone
   would leave a base that is on with nothing else, which a later clear could
   not restore.
   `replay = True` if `call.first_seen < rec.last_layers_change` (a retry loop
   re-applying a press made before the lamp's layers last changed); the engine
   then uses `apply_replay`.
7. `EXTERNAL` via context — `ev.user_id` or `ev.parent_id` set but not in the
   map → `source = user if user_id else automation`, `groups = None`.
8. No usable context:
   1. `rt.room_call` reaches this lamp and the report's on/off does not
      contradict the call's intent → `EXTERNAL` from that call, with its source
      and groups as in step 6. (The engine drops a lamp's room call when a
      render of ours starts on it: our newer command's reports are ours.)
   2. `rt.returning` → `NOISE`: `decide_return` judges the lamp.
      `rt.render_alive` with `rec.last_command` ours (or none): `NOISE`, our
      render's verification judges the lamp (and retries) — except, once the
      lamp has ARRIVED, a report without our context that keeps our target's
      on/off but moves its brightness or colour further from the target than
      the report before (`_further`, beyond tolerance): that is a person at a
      dimmer while we render, → `EXTERNAL(source=device)`, and the engine
      cancels the render instead of re-sending over them six times. ARRIVED:
      the lamp has shown the target for `ARRIVED_HOLD_S` with no other report
      in between (`last_command.matched_at`, kept by the engine from the lamp's
      own reports, not persisted). It is never measured from when the command
      was sent: a lamp may answer seconds late (a lossy mesh, a retransmission
      past Home Assistant's 5 s context reuse) and may first echo a remembered
      level — the target itself, for a nightlight — then jump to its power-on
      level and fade. Before arrival a move away is the lamp still answering;
      a person who moves a dimmer then is sent over once by the verification
      and recognised from their next move. A flip stays noise (a bridge's
      optimistic off corrected later); a step towards the target is a
      transition; a report carrying our context inside Home Assistant's 5 s
      reuse is the lamp answering our call (one that clamps what it was sent)
      and is left to the verification.
   3. **Late window** — `rec.last_command` within `LATE_WINDOW_S[platform]`
      (default `LATE_WINDOW_DEFAULT_S`, both inclusive), the new state flips
      on/off against `last_command.target`, back to `last_command.from_state`
      (when known), and the command was ours or re-sending it would not light
      the lamp (`not raises_output(new, target)`) → `FAILED_DELIVERY(ours=last_command.ours)`.
      Only flips back to where the lamp was count: those are the measured
      reversals. A brightness or colour move with no context is a person's (a
      dimmer bound to a vendor bridge) and is never retried; so is an off after
      a command that only dimmed a lamp that was on, and an on after an off that
      found the lamp already off. A reverted foreign ON is judged as a change
      (steps 4-6): marking it `delivery` would make the next `layers.*` call on
      the lamp re-send it and light a lamp someone may have switched off.
   4. `rt.render_alive` → `NOISE`.
   5. `rec.last_external` within `FOLLOW_UP_S` (inclusive), no on/off flip, no
      layer change since (`last_layers_change` not after it) and no command of
      ours sent since → `FOLLOW_UP` (re-apply `last_external.policy` with its
      `groups`, silently).
   6. otherwise → `DEBOUNCE`: the engine stores `observed_prev` (if not already
      debouncing) and calls `settle_debounce` after `DEBOUNCE_S`.

### 6.2 `settle_debounce(rec, current, caps, now=None) -> Verdict`

- `current` missing or unavailable → `IGNORE` (the transport path has it).
- `current` close to `rec.observed_prev` → `REREPORT` (a blip that came back).
- `current` matches (`yes`) the projection of the lamp's effective command →
  `REREPORT`. `colour_off` does not count: a colour someone changed is theirs.
- otherwise → `EXTERNAL(source=device, groups)`: the groups that changed from
  `observed_prev` (`changed_groups`: `state` plus `brightness` and/or `color`,
  whichever moved beyond tolerance), or `None` (every group shown) when the lamp
  flipped on/off or `observed_prev` is unknown. A dimmer turning down a lamp
  that shows a signal colour makes the brightness the base's, not the colour
  (the take-back is then `partial`, and the next targeted call repairs it).

### 6.3 `classify_call(rec, call, caps, now) -> Verdict | None`

The intent path, for changes that produce no `state_changed`. The verdict's
groups are the call's plus `state` (for an off intent, `{state}`).

- lamp unavailable and the intent is known (an off, or a `turn_on` with a
  command) → `EXTERNAL_INTENT` (the engine records the intent under the lamp's
  policy and sets `owed(missed=True)`; see 6.4). A toggle or an unknown intent
  has nothing to record → `None`.
- `turn_off` (or a `turn_on` to brightness 0) and the lamp shows `off` →
  `EXTERNAL_INTENT(command=off)`.
- `turn_on` naming brightness and/or colour the lamp already shows —
  `matches(O, project(on + those groups))` is `yes` and `compared_groups` is not
  empty, so at least one of them is something the lamp takes and can be
  compared on (a colour a CT-only lamp cannot show, hs/rgb, or a brightness to
  an on/off lamp would otherwise match vacuously) → `EXTERNAL_INTENT(command=call.command)`.
- anything else (a bare `turn_on`, a toggle, an unknown intent, a call not
  reaching the lamp) → `None` (the state path will see the change).
- `replay` as in 6.1 step 6; the engine uses `apply_replay` for it.

### 6.4 `decide_return(rec, O, caps, now) -> ReturnDecision`

Run `RETURN_SETTLE_S` after a lamp comes back, and at the end of the startup
grace for lamps with `owed`. `E = resolve(rec, now).command`, `P =
rec.p_at_drop` (unknown when missing or unavailable). "Lights" means sending E
would turn O on or brighten it (`raises_output(O, E)`). First match wins:

- O missing or unavailable → `WAIT` (keep `owed`).
- `E is None` → `RECORD` (base := O).
- `matches(O, project(E))` is `yes` → `NOTHING` (clear `owed`).
- `P` known and not `close(O, P)` (it came back different: a power cycle at a
  switch, a vendor app) → `EXTERNAL(source=device)`.

From here nobody touched it (`P` close) or nobody can tell (`P` unknown):

- nothing owed and `diverged` is `manual_keep` or `unsynced` → `NOTHING`: only
  `layers.sync` pushes those (a render owed since then is still delivered).
- a live `set` layer holds the lamp (`state_holder`) → `SEND`.
- an `adjust` layer is active and O is on (sending only changes its
  attributes), and `P` is known or E does not light → `SEND`.
- `owed`, and E does not light, or `P` is known and `now - owed.since <=
  OWED_ON_MAX_AGE_S` → `SEND`. (An owed off, or a release that only dims, is
  delivered however old; only a render that would light a lamp ages out.)
- otherwise → `RECORD` (and clear `owed`).

`EXTERNAL` needs evidence of a change, and `SEND` without evidence that the
lamp was untouched is limited to a holding layer or a render that cannot light
it. That is why an Off pressed while a lamp was away since before startup is
still delivered, and why an adjust layer never lights a lamp on its return.

## 7. Engine (Home Assistant side)

### 7.1 Listeners

- `async_track_state_change_event` on enrolled lamps only.
- `hass.bus.async_listen(EVENT_CALL_SERVICE, handler, event_filter=@callback f)`,
  where `f` keeps `domain == "light"` and `service in {turn_on, turn_off,
  toggle}`. The handler normalises the raw data: `cv.comp_entity_ids` first (so
  `ALL` is all), `entity_id: all`, area/device/label via
  `async_extract_referenced_entity_ids` (with `expand_group`, so old-style
  `group.*` entities reach their members, as in the light service), light
  groups and vendor room groups expanded through their `entity_id` attribute,
  `brightness_pct` → brightness, `color_temp_kelvin`/`xy_color`/`hs_color`/
  `rgb_color`; `brightness_step*`, `profile`, `flash`, `color_name`, `white`
  make the intent unknown (state path only). Calls with our own context ids are
  ignored. Each foreign call is kept for `CALL_MEMORY_S` in the call map, and
  `last_command` is updated on every lamp it targets (see 2).
  `CallInfo.first_seen` is the first time that call was seen, remembered for at
  least 64 s across re-sends (6, the replay rule). A call is keyed by context
  id, service, intent, groups and lamps: a retry loop re-sends the same call,
  while a script's next step under the same context is a new one.
- The call event fires before the light service checks the caller's
  permissions. A call with a `user_id` is only acted on for lamps that user may
  control (`POLICY_CONTROL`; admins may control all); a call from an unknown user
  is ignored. The user is looked up in an eager task, which completes inside the
  event (the lookup does not suspend), before the call writes any state.
- A second call listener keeps `domain == "scene"` and `service in {turn_on,
  apply}` (`scenes.py`). Home Assistant's scenes send nothing to a member that
  already matches (light and switch `reproduce_state` return early), so neither
  path above would hear of it. The engine reads what the scene wants of each
  enrolled member (`scene_config.states` of Home Assistant's own scenes, or
  `apply`'s `entities`; a vendor scene lists none), then `SCENE_SETTLE_S` (2 s)
  later takes each member that got no light/switch call under the scene's
  context and builds the call `reproduce_state` would have made. It goes to
  `classify_call` (6.3) first: only an `EXTERNAL_INTENT` is handled, as a call
  (decision `scene_skipped`); anything else is dropped with no trace in the
  record (decision `scene_skip_ignored`). A user's scene reaches only lamps that
  user may control, as above.
- `Runtime.returning` is true from a lamp's `TRANSPORT_UP` until its
  `decide_return` has run.
- `GONE` (the entity was removed: its integration reloading, a deletion) is
  handled like `TRANSPORT_DOWN`. The entity reappearing after the grace on a
  lamp that is away (`FIRST`) is a return, judged like `TRANSPORT_UP`.
- An enrolled lamp whose entity id is renamed is followed: its record moves to
  the new id, the options' lamp lists are updated, and the entry reloads.
- The event filter and handlers must never raise. Every loop over lamps
  isolates each lamp; an exception on a lamp is logged and marks it `untrusted`
  (2), which also cancels its render.
- `apply_external` gets the lamp's `caps` and runs after `rec.observed` holds the
  new report. A verdict with `replay = True` (state path or intent path) goes to
  `apply_replay`, not to the lamp's policy, and schedules the replay re-render
  (7.3 f). A `FOLLOW_UP` re-applies `last_external.policy` with its groups.
- A change on the state or intent path supersedes what Layers had pending on the
  lamp: a debounce, a return decision (and, on an available lamp, `p_at_drop`),
  the replay re-render, our render and its late re-check.
- Before a deferred render (an expiry, the replay re-render, a late re-check, a
  failed-delivery retry) and before a `layers.*` call acts on a lamp, a pending
  no-context change on it is judged first (`settle_debounce`): a person's change
  is never overwritten by a render that was due.
- Our live contexts (`ours`) are handed to the next engine across a reload: Home
  Assistant keeps stamping the lamps' writes with them for 5 s, and an unknown
  one would read as an automation's change (6.1 step 7).

### 7.2 Rendering

- One background task per lamp (`entry.async_create_background_task`), registered
  before its first light call runs (not started eagerly: a lamp can write its
  state from inside that call, and the render must already count as alive). A
  newer render, a take-back, turning the apply switch off, and unload cancel it.
- A render starts only when the lamp's effective command changed (or a targeted
  call repairs a `delivery`/`partial`/`colour` divergence, or `sync`), and only
  if the lamp does not already match. The lamp's report only counts as matching
  when no render of a different command is in flight: that command may already
  have gone out, with the report still to come. Identical in-flight renders are
  kept.
- From the start, and again before each attempt: `owed = Owed(since, target,
  turns_on)`, persisted immediately; a fresh `Context(parent_id=caller_context_id)`
  added to `ours`; `last_command` with the lamp's `from_state`; fire
  `layers_render` with that context (`entity_id`, `layer`, `owner`). The status
  sensor is refreshed each time.
- Saves: model changes after 2 s, observations after 60 s, owed at once. Home
  Assistant's Store keeps a single timer that a later, longer delay postpones,
  so a longer delay is never asked for while a sooner write is pending (the
  pending write takes the latest data).
- `hass.services.async_call("light", service, data | transition, blocking=True,
  context=ctx)` inside a 10 s timeout; any exception from it is swallowed
  (`HomeAssistantError`, `vol.Invalid` and `TimeoutError` quietly, anything else
  with a warning) — verification decides. An unexpected error elsewhere in the
  render ends it as failed.
- Before each retry, and before accepting or failing, the render checks that its
  call is still the projection of the lamp's effective command. If the record
  changed without a new render (an expiry the safety rule did not let render, a
  return recorded as base, a replay), it stops and owes nothing.
- Wait `transition + SETTLE_S` (Hue off: `SLOW_OFF_S`; Matter off: no
  transition wait), then `matches`:
  - `yes` → verified: clear `owed`, delete the repair issue; for platforms with
    a late window, schedule one re-check at `LATE_RECHECK_S`.
  - `colour_off` → accepted after one retry; `diverged = colour`.
  - lamp unavailable, or its state gone (the entity removed: its integration
    reloading), before verification or before a retry → keep `owed`; the
    return handles it. (Without a state the lamp's caps read as empty, and the
    projection would drop the brightness: that is not a changed command.)
  - otherwise retry with `RETRY_BACKOFF_S`; after the last, create a
    non-persistent repair issue and fire `layers_render_failed`. Unloading the
    entry deletes these issues.
- One late-window retry per command: a `FAILED_DELIVERY` of ours is retried once
  (the late re-check spends the same retry). A reversal of that retry is a
  person, not a bridge: it is debounced and judged as a change.
- Every attempt's context stays in `ours` until at least 5 s after the render
  ends (verified, failed or cancelled; the engine uses 10 s), and is never
  dropped at verification: Home Assistant stamps the lamp's writes with it for
  5 s, and a context of ours that has left `ours` would read as an
  automation's change (6.1 step 7) and take back our own layer. The classifier
  only recognises a released context when it is `rec.last_command`'s.

### 7.3 When a command may be sent (the invariant)

Only:
(a) a `layers.set`/`clear` changes a lamp's effective command, or repairs a
    `delivery`/`partial`/`colour` divergence on a lamp it targets;
(b) `layers.sync`;
(c) an expiry `expire()` allows (the safety rule, `on_expire: render`, or a
    clear of a layer that had already run out, 5.2);
(d) a return (or startup `owed`) that `decide_return` answers with `SEND`;
(e) retries of (a)–(d), (f), and `FAILED_DELIVERY` of our own command (once per
    command, 7.2);
(f) the re-render `REPLAY_QUIET_S` after the last replayed foreign call on a
    lamp (5.3 `apply_replay`), putting its layers back;
(g) a `device` change on an entity with the `reassert` policy while a layer is
    active (5.3), after the usual debounce or return settle — the one case an
    external change answers with a command, and only on entities so configured.

Never because of an external change otherwise, never at startup otherwise, never while
the apply switch is off, never to a lamp whose effective command is `None`.
(c)-(f) never push a lamp that is `unsynced` or `manual_keep`, nor one that is
`untrusted`; a render skipped for one of those reasons, or because the apply
switch is off, clears `owed` (only `layers.sync` pushes such a lamp, so
nothing is owed, and the status sensor must not read `pending` until then). Wherever the record changes without a render (the effective command
becomes `None`, an expiry the safety rule does not let render, a return recorded
as base, a replay), a render in flight that sends anything else is cancelled,
and the renderer re-checks its call before each retry (7.2).

### 7.4 Startup and reload

- Load the Store (an owed render is kept however old: 6.4 ages out only an owed
  render that would light the lamp). Records only; nothing is sent. Seed
  `p_at_drop` (2).
- First states (`FIRST`) update `observed`.
- After `STARTUP_GRACE_S` (measured from `EVENT_HOMEASSISTANT_STARTED`, or from
  setup when HA is already running), in this order:
  1. `expire()` every layer that expired while Home Assistant was down (the
     safety rule applies; layers an owner cleared during the grace render,
     5.2). A lamp whose `expire()` allows a render gets that render (owed while
     unavailable). A lamp whose layers expired is done with this step, render or
     not: it is neither judged by `decide_return` (a render is Layers' own
     decision, 7.3 c, not an old debt) nor recorded as `base := observed` (the
     lamp still shows the expired layer, e.g. a signal colour, which must not
     become its base).
  2. An available lamp with a render in flight (a `layers.*` call during the
     grace) is left to that render: its `owed` is no old debt.
  3. Other available lamps with `owed` get `decide_return`.
  4. Other available lamps without layers get `base := observed`.
  5. Other available lamps with a layer that do not match: if what they show is
     not `close` to what they showed when Layers stopped (`p_at_drop`), someone
     changed them meanwhile, and that is an external change from the device
     (the lamp's policy applies, as for a return, 6.4); otherwise `diverged =
     unsynced`.
  A lamp that is away is judged by `decide_return` when it comes back. TTL
  timers that would fire inside the grace are held until it ends.

### 7.5 Apply switch

`switch.layers_apply`, persisted in the Store (saved immediately), off on a
fresh Store. Off: the model still changes, nothing is sent, in-flight renders
are cancelled, and lamps whose effective command changed are marked `unsynced`.
A lamp that was owed something (a render in flight, or one owed while it was
away) is marked `unsynced` and owes nothing: its return sends nothing either.
Turning it on sends nothing; `layers.sync` pushes.

### 7.6 Services

| Service | Fields |
|---|---|
| `layers.set` | target (entity/group); `layer`; `priority` (1–99); `mode`; `state`; `brightness` \| `brightness_pct`; one of `color_temp_kelvin`, `xy_color`, `hs_color`, `rgb_color`; `transition`; `ttl` \| `until`; `resume_after_manual`; `on_expire`; `only_if_present`; `owner` |
| `layers.clear` | optional target; `layer` (id, `active`, `all` — `active` and `all` need a target; `base` is refused); `transition` |
| `layers.sync` | optional target |
| `layers.get` | optional target; response only |

`set`/`clear`/`sync` return (when asked) a per-lamp result: `queued`,
`unchanged`, `in_sync`, `pending`, `shadow`, `skipped_tombstoned`,
`skipped_absent`, `skipped_not_enrolled`. Lamps that are not enrolled are
skipped and reported, never raised on.

### 7.7 Entities and diagnostics

- `switch.layers_apply`; `sensor.layers_status` (`ok` / `pending` / `failed` /
  `shadow`, attributes: `pending_since` per lamp, `failed` lamps (a failed render
  whose lamp is still `delivery`), `untrusted` lamps; lamps unavailable for more
  than 24 h are ignored).
- Diagnostics: options, Store, in-flight renders, and a ring buffer of the last
  500 classifier decisions, with `user_id` and context ids redacted.
- `layers_render`, `layers_render_failed`, `layers_external` events;
  `logbook.py` describes `layers_render` so Activity shows the layer and owner.
