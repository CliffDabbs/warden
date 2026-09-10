> **Legacy — superseded by [Warden](../warden/README.md).** This tool's job
> (one config file reconciled onto AdGuard) is now Warden's AdGuard service plus
> `warden/config/warden.yaml`, live and with no Home Assistant in the loop.
>
> ⚠️ **`deploy.py` still runs, and still writes to the same live AdGuard that Warden now owns.**
> Running it will overwrite Warden's client tags and blocked-service lists. `screentime.yaml`
> has also drifted from `warden/config/warden.yaml` — different Living Room TV address, and
> services listed here that Warden no longer manages.
>
> **Still live:** `deploy.py` is the only code in this repo that writes AdGuard's always-on
> baseline (safe search, parental control, safe browsing). Warden reads that setting but never
> writes it — see [docs/05](../docs/05-adguard-rules.md).

# screentime — parental controls as code

One config file (`screentime.yaml`) → reconciled to **AdGuard** (clients, tags, baseline,
default blocks) and **Home Assistant** (switches, schedules, automations, dashboard).

```
screentime.yaml ──► deploy.py ──► AdGuard  (REST API, GET-merge-PUT)
                              └──► HA files (packages/ + dashboard) ──► reload
```

## Setup (one time)

1. Python 3 (you have it via the `py` launcher). Install deps:
   ```powershell
   py -3 -m pip install -r requirements.txt
   ```
2. `copy .env.example .env` and fill in AdGuard creds + (later) your Samba path.

## Use

```powershell
py -3 deploy.py --dry-run     # preview: AdGuard plan + what HA files would be written
py -3 deploy.py --print       # print the generated HA package to screen
py -3 deploy.py --ha          # generate HA files only (to iac/out until HA_CONFIG_DIR is set)
py -3 deploy.py --adguard     # push client state to AdGuard only
py -3 deploy.py               # everything: AdGuard + write HA files + reload HA
```

Flags: `--reset-services` also forces AdGuard `blocked_services` back to the config
defaults (normally the tool leaves live blocked state alone — Home Assistant owns it at
runtime).

## How the two halves divide responsibility

- **deploy.py** owns *structure*: which clients exist, their tags, the always-on baseline
  (Safe Search / parental / Safe Browsing), the default blocks, and all the HA wiring.
- **Home Assistant** owns *runtime*: flipping services on/off via the switches/schedules.
  Its PUTs send the **full** client object (tags + baseline baked in by the generator), so
  runtime toggles never wipe the baseline.
- Re-running deploy.py is safe: on existing clients it preserves live `blocked_services`
  unless you pass `--reset-services`.

## Going live to Home Assistant

Set `HA_CONFIG_DIR` in `.env` to your Samba share (`\\homeassistant\config`). Then
`py -3 deploy.py` writes:
- `packages/screentime_generated.yaml` (needs packages enabled — see main README)
- `screentime-dashboard.yaml`

First time you add NEW entities, restart HA once; after that `reload_all` (set `HA_URL`
+ `HA_TOKEN`) picks up value changes without a restart.

Add the dashboard in `configuration.yaml`:
```yaml
lovelace:
  mode: storage
  dashboards:
    screen-time:
      mode: yaml
      title: Screen time
      icon: mdi:television-guide
      filename: screentime-dashboard.yaml
```

## Adding things

- **A device** → add a line under `clients:`.
- **A service** → add a line under `services:` (valid AdGuard id) with its groups.
- **A schedule / override** → add under `schedules:` / `overrides:`.

Re-run `deploy.py`. Everything regenerates. Never hand-edit `screentime_generated.yaml`.

## Note

`screentime_generated.yaml` supersedes the hand-written files in `../packages/`
(`adguard_service_switches.yaml`, `schedules.yaml`). Once you're on the generated version,
remove those to avoid duplicate entity definitions.
