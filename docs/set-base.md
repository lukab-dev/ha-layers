# `layers.set_base`: a scene that goes through Layers

Status: implemented on branch `set-base` (not released). The contract is in
[SPEC.md](SPEC.md) 5.6, 7.3 (a) and 7.6; this page is the reasoning behind it.

## The problem

Layers watches everyone else's commands; it does not intercept them. A scene that
reaches a managed lamp is a change Layers did not make, and Layers only learns what
it did from the lamp's reports. Three things follow from that:

1. **Two steps where one would do.** A scene turns an off lamp on at 255, and the
   lamp has a follow layer (say a colour that follows the time of day). The scene's own `turn_on`
   goes out first; Layers sends the follow colour `FOLLOW_SETTLE_S` (2 s) after
   the last report of it (SPEC 7.3 i). Before that wait, the first report of a
   fading lamp (Matter: "on" at its old level) became the base and was sent
   straight back, which stopped the fade at the old level. The wait fixes that,
   at the cost of a visible second step.
2. **A dip under a sticky or locked layer.** The scene's command lands on the
   lamp, and Layers puts the layer back `HOLD_SETTLE_S` later (7.3 j): every press
   flashes the scene for about 2 s.
3. **No delivery check.** Layers checks that *its own* commands arrived (7.2) and
   retries them once if a bridge reverts them late. A scene's command gets none
   of that: a lamp that reverts is read as a change at the device and becomes the
   new base.

All three come from Layers seeing the scene after the lamp did. The fix is to
let a button ask Layers instead: "the base is now this scene". Layers can then
work out what each lamp should show, with every layer on it, before anything is
sent.

`layers.set_base` is opt-in: it is for the buttons and automations someone points
at it. Wall switches, voice, the app and other automations keep reaching the
lamps directly, and the settle wait stays as the safety net for them.

## The service

```yaml
action: layers.set_base
target: {entity_id: scene.evening}     # one or more scenes (areas and labels work too)
data:
  transition: 1                        # optional, as for scene.turn_on
```

or, in `scene.apply`'s format, without a scene entity:

```yaml
action: layers.set_base
data:
  entities:
    light.lamp_a: {state: "on", brightness: 255}
    light.lamp_b: "off"
```

It takes the scene as a scene, not the lamps as a target: the point is that one
button press does what `scene.turn_on` would, with Layers in the middle for the
lamps it manages.

## Decisions

### How the scene's contents are read

With `scenes.py`, the same code that already reads a scene for the members Home
Assistant skips (SPEC 7.1): `scene_config.states` of Home Assistant's own scenes
(YAML, the editor, `scene.create`), or `entities` given inline. For each enrolled
member, `reproduced_call` builds the call Home Assistant's `reproduce_state` would
make, and `normalise_call` turns it into a command and the groups it names, as for
any call. So `set_base` reads a scene exactly as Layers already reads the same
scene when it runs on its own.

- Several scenes: later ones win per entity, as `scene_targets` already does.
- **Members Layers does not manage** (other lights, covers, media players) are
  passed on to `scene.apply` in one call, under the caller's context, so Home
  Assistant applies them and checks the caller's permissions as usual. A
  managed lamp whose call has no known intent (`effect`, `white`, `rgbw_color`,
  `rgbww_color`: SPEC 7.1) is passed on with them: Layers cannot say what that
  lamp will show, so it lets the scene send it and watches, as today.
- **A scene with no members Layers can read** (a vendor scene run by a bridge) is
  passed on to `scene.turn_on`. The response names everything passed on.
- A light group in a scene is passed on: Home Assistant fans it out, and the call
  path sees its members as for any call. Name the lights in the scene to route
  them through Layers.

### What the change does to the layers: "as if made directly"

Each enrolled lamp goes through `apply_external` (5.3), the same function a
direct scene call reaches, with the inputs the direct path would give it:

| Input | Value | Why |
|---|---|---|
| `shown` | the scene's command for the lamp | the intent path's `shown` (6.3) |
| `groups` | the groups the scene names, plus `state` | a `turn_on`/`turn_off` always decides the state (6.1 step 6) |
| `source` | `user` if the call has a `user_id`, else `automation` | as for any call |
| `policy` | the lamp's own | `reassert` turns into `take_back` for a non-device source, as before |
| `scope` | `room` | a scene is aimed at the room (6.5), whoever pressed it |
| `chosen` | the brightness/colour the scene names | see below |

So soft layers are dropped and tombstoned, or edited, as the lamp's policy says;
sticky and locked layers are lifted out, the change goes into the base underneath,
and they are put back; `layers_external` fires when a layer was dropped, edited or
held. A button routed through Layers changes the stack the way the same button
calling `scene.turn_on` would. What differs is only what reaches the lamp, and
when.

### Which groups count as "chosen" for follow layers

The groups the scene names for that lamp, of `brightness` and `color`, and
nothing else. This is what the direct path already uses for a call with a known
intent (`_chosen`: "a call with a known intent: the groups it named"). Each follow
layer stops following those until the lamp is seen off (or its
`manual_timeout`), and follows the rest: also what an earlier scene or a hand had
taken over. A scene says everything about the lamp, so `set_base` first releases
every follow layer on it (as "seen off" does) and then applies what this scene
names. Otherwise the same scene gives the follow colour from off, and the previous
scene's colour when pressed after it.

- A scene naming `brightness: 255` and no colour leaves the follow layer's colour
  following: the lamp gets 255 with the follow colour, in one command.
- A bare `on` names nothing: the follow layer gives both. The direct path does the
  same for a lamp that just came on ("it shows its power-on level").
- A scene the editor captured names a colour mode, so it names a colour: the
  colour stops following, exactly as the same scene does through `scene.turn_on`.
  To keep the colour following, leave the colour out of the scene (YAML, or
  `entities`).

The groups *taken into the base* differ from the direct path in one case. A lamp
turned on from off by a call naming only its brightness reports every group it
shows, and the direct path takes them all (6.1 step 6: the lamp coming on decided
its colour too). Here nothing is reported before the base is recorded, so the base
takes only what the scene names. See "What the base records" for the gap this
leaves and how it closes.

### What reaches a lamp held by a sticky or locked layer

Nothing visible. After `apply_external`, Layers sends the lamp its **effective
command**: the new base with every layer on top resolved over it. Under a strong
layer that covers the brightness and the colour, that is the layer's own command,
which the lamp already shows, so nothing is sent (`in_sync`). A strong layer that
covers only some groups (a `set` layer with a brightness alone) gets one command
that changes only what the layer leaves to the base. Under a held `off` nothing is
sent either. No hold re-show (7.3 j) is scheduled: there is nothing to undo.

`apply_external` marks a held lamp `delivery` when it does not show the effective
command yet, and `base_keep_layers` marks `manual_keep`. Both describe a lamp that
already took a change Layers did not make. Here nothing has been sent yet, so
`set_base` keeps the lamp's divergence as it was before the call, and the render
that follows settles it like any targeted call: verified clears it, failed sets
`delivery`.

`base_keep_layers` therefore does not show the scene over a layer: the layers stay
and the lamp shows its effective command. That policy shows a person's change
because Layers will not fight one already on the lamp; through `set_base` nothing
is on the lamp yet, and the caller asked Layers for the result.

### What the base records, and what is sent

- **Recorded:** `base = merge_command(base, scene command, groups)`: the scene's
  values only, never what a layer adds. The follow colour stays in the follow
  layer, so when the follow layer goes the lamp falls through to the scene's
  colour (or its own).
- **Sent:** `project(resolve(rec))`, through `_render` with `reason: set_base`, the
  call's `transition`, and the caller's context as parent. One command per lamp,
  combining the scene with every live layer. It is sent even when the effective
  command did not change (an `unsynced` lamp, a `delivery` divergence), and
  skipped when the lamp already shows it.
- **Checked:** it is a render like `layers.set`'s (7.3 a): owed until verified,
  retried with backoff, one late retry if a bridge reverts it (7.2), owed to a
  lamp that is away, and nothing at all with Apply off (the lamp is marked
  `unsynced`). This is the roadmap's delivery-check item for buttons routed
  through Layers. The other half, a late retry for plain `scene.turn_on`, is not
  part of this.
- A lamp that was off has an `off` base, which carries no brightness or colour. A
  scene naming only a brightness leaves the base with no colour, and if no layer
  gives one either, the lamp comes on in the colour it remembers, which Layers
  only sees in reports of its own command. **Gap fill:** when a `set_base` render
  is verified, a group that both the base and the effective command lack is
  learned into the base from what the lamp shows. Otherwise a colour layer
  cleared later would have no colour to restore: the direct path avoids the same
  hole by taking every group from a lamp that just came on. The late re-check
  then checks the filled command, which the lamp already shows.

### Permissions

`scene.turn_on` from a person reaches only what that person may control: Home
Assistant checks the scene, and each member's light call. `set_base` sends the
managed lamps' commands itself, under a context of its own, so it checks first: a
non-admin user's call is refused before anything changes unless they may control
every scene named, every lamp Layers sends to and every member passed on. The
pass-on runs under the caller's context, so Home Assistant checks it again.

### Why not intercept `scene.turn_on`

Layers could wrap or replace the scene service and route every scene through
itself. It does not: it would change what an existing Home Assistant service does
for everyone who installs Layers, would break on every change to the scene
integration, and would remove the one thing that makes Layers safe to add to a
working house, that nothing changes until an automation calls `layers.*`.
`set_base` keeps that: nothing goes through it unless someone points a button at
it.

## Not done

- Areas, labels and devices of *lamps* as a target: the target is scenes. A scene
  is how a button names "these lamps like this".
- Plain `scene.turn_on`: no late retry and no `diverged: delivery` (roadmap item 2).
- A `set_base` that names lamps and a command without a scene (`layers.set` with
  `layer: base` does that, without the policy).
