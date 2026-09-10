> **Legacy — the Home Assistant era.** Superseded by
> [Warden](../warden/README.md): schedules are now plain-English rules on a cron trigger, and
> overrides are quick actions with a timed revert. Ignore anything below claiming Home
> Assistant owns the runtime. Not maintained, not deployed.

# Scheduling in Home Assistant

Automations handle the recurring rules. The trick that keeps it clean:

> **Schedules flip the `input_boolean` switches — nothing else.**
> The sync automations in `adguard_service_switches.yaml` and `kids_tablets.yaml`
> already react to those booleans and push the change to AdGuard / UniFi.
> So the switch is the single source of truth, and the dashboard always matches reality.

See `packages/schedules.yaml`.

## The Schedule helper (edit bedtime without touching YAML)

`schedule.kids_screen_time` and `schedule.tablet_allowed` are **Schedule helpers**: a weekly
calendar you drag-edit in the UI (Settings → Devices & Services → Helpers → click the schedule).
The entity is **on** during its blocks, **off** otherwise. An automation turns the switches on
when the window opens and off when it closes.

Defaults shipped: kids' AdGuard services allowed weekday afternoons / weekend days; tablets
allowed 07:00–20:00ish. Change them in the UI — no restart needed.

## Overrides

- **Holiday mode** (`input_boolean.holiday_mode`): while on, the "block at end of window" step is
  skipped, so bedtime blocks don't fire. When you turn it off, everything snaps back to whatever
  the schedules currently say.
- **Movie night** (`input_boolean.movie_night`): allows TV YouTube/Netflix now, auto-clears at 23:30.

## Interaction to know about

Schedules **re-assert at each boundary**. If you manually allow something mid-window, the next
schedule edge will set it back to the scheduled state. That's usually what you want ("20 more
minutes" then it re-blocks). For a longer manual override, use holiday/movie-night mode.

## The scheduling toolbox (triggers you can use)

| Trigger | Use for |
|---|---|
| **Schedule helper** (`state` of a `schedule.` entity) | Weekly, UI-editable windows — the default here |
| `time` (`at: "20:00:00"`) | A fixed clock time |
| `time_pattern` (every N min) | Periodic checks (e.g. re-assert every hour) |
| `calendar` | Event-driven — e.g. a "No screens" Google/local calendar |
| `sun` (sunset/sunrise) | Lights, blinds, anything tied to daylight |

Add conditions for weekday/weekend (`condition: time` with `weekday:`), someone home
(`condition: state` on a person), etc.

## Extending

- **Homework hour:** add a `schedule.homework_time` and an automation that turns kids' distracting
  services off while it's on (layer it after screen time).
- **Per-kid schedules:** duplicate a schedule + automation targeting that child's booleans/clients.
- **Energy scheduling** (project C) works the same way — a schedule or time trigger flips a boolean
  that an automation acts on.
