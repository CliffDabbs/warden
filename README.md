# Home Automation

## → The project has pivoted to **[Warden](warden/README.md)**

**[`warden/`](warden/)** is the new primary system: a first-class, containerised
**web hub** that gives web controls over AdGuard Home ("adaware"), a **plain-English
rules engine**, and **pluggable website scrapers** that feed it — no Home Assistant in
the loop.

```
"When Luke has completed his Atom Learning, allow YouTube for the kids."
"At 8pm every day, disable all kids devices."
```

Quick start: `cd warden && py -3 -m venv .venv && .venv\Scripts\python.exe -m pip install -r requirements.txt && .venv\Scripts\python.exe -m warden` → <http://localhost:8080>.
Or `docker compose up -d --build`. See **[warden/README.md](warden/README.md)** and
**[warden/INTERFACES.md](warden/INTERFACES.md)**.

Warden folds in the two good ideas below: the AdGuard "controls as code" (`iac/`) became
Warden's AdGuard service + `config/warden.yaml`, and the multi-source digest contract
(`docs/sourceadapterCONTRACT.md`) became Warden's pluggable source adapters + signal bus.

---

## Legacy / reference — the original Home Assistant projects

Kept for reference; Warden supersedes the runtime pieces (HA packages, schedules).
Three projects, each self-contained. A is the fiddliest, C is the most fun.

| # | Goal | Main tools | Folder |
|---|------|-----------|--------|
| A | Block YouTube for kids, allow on TVs on demand | pfSense + AdGuard Home + Home Assistant | `docs/01-youtube-blocking.md` |
| A+ | Organise AdGuard rules: kids locked down, per-service switches in HA | AdGuard + Home Assistant | `docs/05-adguard-rules.md` |
| A++ | Schedule screen time (bedtime, homework, holiday/movie overrides) | Home Assistant | `docs/06-scheduling.md` |
| IaC | **Parental controls as code** — one config → AdGuard + HA (switches, schedules, dashboard) | Python + AdGuard + HA | `iac/README.md` |
| B | Block the kids' tablets (schedule + ad-hoc) | UniFi + Home Assistant | `docs/02-kids-tablets.md` |
| C | Charge batteries on Octopus free-electricity sessions | Octopus + Solar Assistant + Home Assistant | `docs/03-octopus-free-charging.md` |

## Home Assistant packages

The `packages/` folder holds ready-to-edit HA config. To use them, enable packages once in `configuration.yaml`:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

Then copy the files from this repo's `packages/` into your HA `config/packages/` folder and edit the **`# TODO`** placeholders (IPs, entity IDs, MQTT topics, credentials).

## Things I need from you to finalise the placeholders

1. **Static DHCP reservations + IPs** for: each Samsung TV, each kids' tablet. (Set these in pfSense → Services → DHCP.)
2. **AdGuard host/port + a username/password** once the add-on is installed.
3. **The exact HA entity IDs** the integrations create, specifically:
   - UniFi block-client switches (e.g. `switch.kids_ipad_block_client`)
   - Octopus free-session sensor (e.g. `binary_sensor.octopus_energy_<acct>_free_electricity_session`)
   - Solar Assistant battery SoC sensor (e.g. `sensor.battery_state_of_charge`)
4. **The Solar Assistant MQTT control topic** for grid charge on your Sunsynk (find it with MQTT Explorer — see project C).

Paste those back and I'll fill everything in.

## Security note

Everything here is for **your own network and your own kids' devices** — parental controls and energy automation. No third-party targeting, no evasion of anyone else's controls.
