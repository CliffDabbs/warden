# Warden

A parental-control hub for one household. It runs as a single container on your LAN and
gives you **web controls over AdGuard Home**, a **plain-English rules engine**, a
**school-day Reminders page**, and **pluggable scrapers** that read your kids' school and
learning portals to decide when the rules apply.

```
"At 8pm every day, disable all kids devices."
"Block streaming until Luke is on track for his Atom learning. No need to check
 while he's at school; on school days start checking at 4pm, hourly until 7pm."
```

You type rules like that. Warden compiles each one, works out whether it is a matter of
the clock or a matter of live data, and runs it.

**→ [warden/README.md](warden/README.md)** — what it is and how it works
**→ [warden/USER-GUIDE.md](warden/USER-GUIDE.md)** — running it day to day
**→ [warden/INTERFACES.md](warden/INTERFACES.md)** — API and extension reference

## Quick start

```bash
cd warden
cp .env.example .env          # set ADGUARD_PASS; WEDUC_* if you want the school sources
docker compose up -d --build
```

Or locally on Windows:

```powershell
cd warden
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python.exe -m warden
```

Either way: <http://localhost:8080>.

**It boots with nothing configured.** No AdGuard reachable → an in-memory fake, so the
whole UI still works. No LLM backend at all — no `ANTHROPIC_API_KEY` *and* no `claude` CLI on PATH — → rules compile with a built-in parser, and
the Reminders page falls back to the part it can compute in plain code. No captured
portal session → each source runs offline fixtures on demand. What you lose without an
LLM backend is specifically the **AI-evaluated rules** and the newsletter reading; the
clock-driven rules and the whole control surface carry on.

Set `WARDEN_PASSWORD` in `.env` if you want a login in front of it — it is **open access
by default**, which is fine behind a VPN and not fine otherwise.

## What's in the repo

| Path | |
| --- | --- |
| **[`warden/`](warden/)** | **The product.** FastAPI + a no-build SPA, the rules engine, the source adapters, the AdGuard and SSH drivers. |
| [`docs/`](docs/) | Design briefs — one current ([Amazon recon](docs/amazon-parent-dashboard-RECON.md)), four historical, plus the legacy Home Assistant guides. |
| [`iac/`](iac/), [`packages/`](packages/) | Legacy. The Home Assistant era this pivoted away from — see below. |
| `warden/data/` | Git-ignored: the SQLite DB, captured portal sessions, cached newsletters, logs. |

## The three sources it reads

| Source | Signing in |
| --- | --- |
| **Atom Learning** | Google SSO, so you capture the session by hand **once** (`python -m warden.adapters.atom.login`). After that it is headless. |
| **Weduc** / ReachMoreParents | Username and password in `.env`. Captures and silently re-captures its own session — nothing to do by hand. |
| **ParentPay** | **No password of its own.** Warden reaches it through Weduc's partner-login handoff, on credentials it already has. |

Adding a fourth means writing one adapter against
[`warden/adapters/base.py`](warden/warden/adapters/base.py) and adding a block to
`config/warden.yaml`. The core never changes.

## What it cannot do

DNS filtering stops a device looking up a name. It cannot stop a device that already
knows an address — which is why Warden also ships an **SSH lever** to stop the Plex
service on the NAS outright, and why a Fire tablet playing a film it has already
downloaded is out of reach of all of this. [The Amazon recon
brief](docs/amazon-parent-dashboard-RECON.md) is the honest account of that gap; no
adapter is built for it yet.

## Where this came from

This repo started as three Home Assistant projects. Warden replaced the runtime pieces
and folded in the two ideas worth keeping:

| Old | Became |
| --- | --- |
| `iac/deploy.py` reconciling `screentime.yaml` → AdGuard + HA | Warden's AdGuard service + `config/warden.yaml`, live and with no HA |
| The multi-source digest contract ([`docs/sourceadapterCONTRACT.md`](docs/sourceadapterCONTRACT.md)) | Warden's pluggable source adapters + signal bus |
| HA automations and schedules | Warden's plain-English rules engine |

The originals are kept for reference and are **no longer maintained or deployed** — each
of those docs carries a banner saying so. (The HA config in `packages/` does not; it is
listed as legacy here and nowhere else.) They are still the only record of a few things Warden
depends on but does not implement: the DNS-bypass countermeasures at the firewall
(port-53 redirect, DoH/DoT, QUIC) in [docs/01](docs/01-youtube-blocking.md), and the
AdGuard always-on baseline in [docs/05](docs/05-adguard-rules.md), which Warden reads
from config but never writes.

| # | Goal | Folder |
|---|------|--------|
| A | Block YouTube for kids, allow on TVs on demand | [docs/01-youtube-blocking.md](docs/01-youtube-blocking.md) |
| A+ | Organise AdGuard rules: kids locked down, per-service switches | [docs/05-adguard-rules.md](docs/05-adguard-rules.md) |
| A++ | Schedule screen time (bedtime, homework, holiday overrides) | [docs/06-scheduling.md](docs/06-scheduling.md) |
| IaC | Parental controls as code — one config → AdGuard + HA | [iac/README.md](iac/README.md) |
| B | Block the kids' tablets (schedule + ad-hoc) | [docs/02-kids-tablets.md](docs/02-kids-tablets.md) |
| C | Charge batteries on Octopus free-electricity sessions | [docs/03-octopus-free-charging.md](docs/03-octopus-free-charging.md) |

Project C was never finished and nothing supersedes it —
`packages/octopus_free_charge.yaml` is still a template full of `# TODO`s.

## Security & scope

Everything here controls **your own network and your own kids' devices**. The scrapers
sign into **your own accounts**, read-only — they never book, pay or submit anything.
Credentials live only in `.env`, which is git-ignored. No third-party targeting, no
evasion of anyone else's controls. Rotate `ADGUARD_PASS` if it has ever been pasted
somewhere it shouldn't have been.
