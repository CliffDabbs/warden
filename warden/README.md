# Warden

A first-class web hub for parental controls. It gives you **web controls over AdGuard
Home** ("adaware"), a **plain-English rules engine**, and **pluggable website scrapers**
that feed the rules — all in one container. No Home Assistant in the loop.

> "When Luke has completed his Atom Learning, allow YouTube for the kids."
> "At 8pm every day, disable all kids devices."

You write rules like that. Warden compiles each into a typed trigger + actions and runs
them against AdGuard directly.

---

## Why this exists / what it replaces

This repo used to drive AdGuard through Home Assistant (see `../packages`, `../iac`,
`../docs`). Warden folds the two good ideas from that work into one product:

| Old piece | Became |
|---|---|
| `iac/deploy.py` reconciling `screentime.yaml` → AdGuard + HA | Warden's **AdGuard service** + `config/warden.yaml` (live, no HA) |
| The multi-source **digest platform** contract (`docs/sourceadapterCONTRACT.md`) | Warden's **pluggable source adapters** + **signal bus** |
| HA automations / schedules | Warden's **plain-English rules engine** (cron + signal triggers) |

## Architecture

```
 Web UI (no-build SPA)  ── /api + /ws ──►  FastAPI
        │                                    │
        │              ┌─────────────────────┼───────────────────────┐
        ▼              ▼                     ▼                        ▼
   Dashboard     AdGuard service       Rules engine            Source registry
   Rules         (live + fake)         + scheduler             (pluggable)
   Sources        clients/services     plain-English → AST      ┌ atom  (Playwright)
   Activity       rules/protection     cron + signal triggers   └ weduc (Playwright)
                       │                     ▲                        │ emits
                       ▼                     │ reacts                 ▼
                AdGuard Home @10.7.11.29   Signal bus ◄──────────  Signals
                                           (latest + history)     SQLite (state)
```

- **Sources are pluggable.** Adding one (e.g. a rugby-club WhatsApp export) means writing
  one adapter to `warden/adapters/base.py` and adding a block to `config/warden.yaml` —
  the core never changes. `atom` and `weduc` ship as examples.
- **Signals are the bridge.** An adapter emits typed facts like
  `atom.luke.daily_complete = true`; rules react to them.
- **Runs anywhere.** With no AdGuard reachable it uses an in-memory *fake*; with no
  Anthropic key the rule compiler uses a deterministic parser; with no browser the
  scrapers use fixtures. So it always boots and demos.

## Quick start (local)

```powershell
cd warden
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env        # set ADGUARD_PASS (and optionally ANTHROPIC_API_KEY)
.venv\Scripts\python.exe -m warden
```

Open <http://localhost:8080>.

## Quick start (container)

```bash
cp .env.example .env          # fill in creds
docker compose up -d --build
```

The image is based on `mcr.microsoft.com/playwright/python`, so the headless scrapers
have Chromium ready to go.

## Configure it — `config/warden.yaml`

One file defines your **groups** (kids/tv + always-on baseline), **clients** (real
devices, matched to AdGuard by name), **services** (the on/off toggles), **subjects**
(people), **sources** (scrapers + cron), and the **signal vocabulary** the rule compiler
understands. Edit it and Warden reflects the change. Secrets never live here — they come
from `.env` via the names in each source's `secrets:` map.

## How a rule works

1. You type English in the **Rules** tab. Warden shows a live compiled preview.
2. On save it's compiled to a `CompiledRule` (trigger + conditions + actions) and stored.
3. **Schedule** triggers ("at 8pm every day") run on a cron scheduler.
   **Signal** triggers ("when Luke has finished Atom Learning") fire when the signal bus
   sees the matching transition.
4. Actions call AdGuard: allow/block a service for a group, disable a whole group
   ("all kids devices"), toggle protection, or add/remove a filtering rule.
5. Every action is written to the **Activity** log and pushed live to the UI.

## Two kinds of rule — deterministic and AI-evaluated

Warden splits rules by whether they depend on live data:

- **Deterministic (⏰)** — pure time/action rules like *"at 8pm every day, disable all kids
  devices"* run on an exact cron schedule. No LLM, no cost, precise to the minute.
- **Dynamic (🧠)** — rules that reference someone's live progress (*"switch on YouTube when
  Luke has done 30 mins and scored 80%"*) are **evaluated on the fly by the LLM**. Each cycle
  Warden hands the model (a) the rule, (b) the live **state of play** from the sources (Atom:
  today's minutes, topics, assignments…), and (c) the **menu of switches** with their current
  state — and it returns which switches to flip, *from that menu only*. There is no pre-baked
  `daily_complete`-style signal; the condition is judged live.

Dynamic rules re-evaluate on **every source refresh** plus a periodic tick
(`WARDEN_EVAL_INTERVAL_MIN`, default 10 min). Decisions are **idempotent** (no-op if a switch
is already where it should be), **validated** (only real groups/services), and **audited with
the reasoning** — which also shows on the rule card (*"last: Luke has done 0 min today; the rule
requires 30"*). Dynamic rules need an LLM backend (API key or the Claude CLI); with neither they
are skipped. The compiler auto-classifies: mention a person or words like minutes/score/done/
practice and a rule becomes dynamic; otherwise it stays deterministic.

## Sign-in (basic auth)

Set `WARDEN_PASSWORD` (and optionally `WARDEN_USERNAME`, default `admin`) in `.env` to
put a login in front of everything — the UI, the API, and the live WebSocket. The session
cookie is signed and lasts ~10 years, so you sign in **once per device**. Leave the
password empty for open access (e.g. behind a VPN). "Log out" lives on the About tab.

## Rule-compiler backend — API key *or* your Claude Code login

`WARDEN_LLM` picks how English becomes rules:

| value | uses |
|---|---|
| `auto` (default) | API key if set, else the Claude Code CLI if installed, else the parser |
| `api` | Anthropic API (`ANTHROPIC_API_KEY`) |
| `cli` | the local **`claude`** CLI (`claude -p`) — reuses your Claude Code login, **no API key, no extra billing** |
| `off` | the built-in deterministic parser only |

The deterministic parser already handles the common phrasings offline; a backend only
adds understanding of looser wording. The active backend is shown on the About tab.

## Atom Learning — first run (Google sign-in)

Atom signs in with Google, which can't be scripted reliably — so you capture the session
**once** and the adapter reuses it:

```powershell
cd warden
.venv\Scripts\python.exe -m warden.adapters.atom.login
```

A browser opens → sign in with Google → it auto-saves the `.atomlearning.com` cookie jar
to `data/atom_state.json`. After that, the adapter pulls Luke's data straight from Atom's
JSON API **headlessly** (Sources → **Run live**) — no browser, so it works in the container
too (copy `data/atom_state.json` into the `/app/data` volume). Re-run the helper when the
session expires; the Sources card tells you when it has.

It reads `islands` (topics completed, with timestamps), `learning-resources` (set work),
`score` (attainment 0–1 overall + per subject + mastery, with monthly history) and
`mock_tests` (exact % correct) — assembling a **state of play** the dynamic rules reason over
(today's minutes, per-topic scores, subject attainment, recent mock results, assignments). Each
change is archived (`GET /api/sources/atom/history`) so Luke's progress accumulates over time.
Until you capture a session, Sources → **Fixtures** runs a realistic offline sample.

## Add a data source (the whole point of "pluggable")

1. Create `warden/adapters/<name>/adapter.py` with `class Adapter(SourceAdapter)`
   implementing `authenticate` / `collect` / `healthcheck`, emitting `Item[]` and any
   `Signal[]` it can derive.
2. Add a `sources:` block and any new `signals:` to `config/warden.yaml`.
3. Reference the new signal in a plain-English rule. Done — no core changes.

## Endpoints

`GET /api/state · /status · /config · /audit` · `POST /api/actions/{service,group,client,protection}` ·
`/api/rules` CRUD + `/preview` + `/{id}/run` · `/api/sources` + `/{key}/run` + `/{key}/items` ·
`/api/signals` + `/emit` · `GET /ws` (live). Full contract in `INTERFACES.md`.

## Security & scope

Everything here controls **your own network and your own kids' devices**. Scrapers log
into **your own accounts** (read-only) with credentials from your `.env`. No third-party
targeting, no evasion of anyone else's controls. Rotate `ADGUARD_PASS` if it has ever been
pasted into a chat.
