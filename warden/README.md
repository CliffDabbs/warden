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
   Reminders     (live + fake)         + scheduler             (pluggable)
   Rules          clients/services     plain-English → AST      ┌ atom  (Playwright)
   Sources        rules/protection     cron + signal triggers   ├ weduc (Playwright)
   Activity                                                     └ parentpay (SSO)
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

Decisions are **idempotent** (no-op if a switch is already where it should be), **validated**
(only real groups/services), and **audited with the reasoning** — which also shows on the rule
card (*"last: Luke has done 0 min today; the rule requires 30"*). Dynamic rules need an LLM
backend (API key or the Claude CLI); with neither they are skipped. The compiler
auto-classifies: mention a person or words like minutes/score/done/practice and a rule becomes
dynamic; otherwise it stays deterministic.

### Dynamic rules schedule themselves

There is **no fixed polling interval**. A rule's English carries its own cadence, and every
evaluation returns a `next_check_at` that the engine arms as a one-shot job:

> "…No need to check while he's at school; on school days don't start checking until 4pm,
> then check hourly until 7pm or until he's finished. At 8pm block all streaming again."

While that instant is in the future the rule is **asleep** — the safety-net tick
(`WARDEN_EVAL_INTERVAL_MIN`, cron-anchored so checks land on the clock) won't wake it, and
nor will a poll that brings back the same facts as the last one. That gating is what makes
"don't check while he's at school" real rather than advisory. The one thing that does wake
it early is **new data**: when a source run changes the state of play, every dynamic rule
re-evaluates immediately, because the alternative is a rule sitting on the answer to its own
question until its next check comes round. Wake-ups are clamped to 5 minutes … 7 days,
persisted, and re-armed after a restart; "Evaluate now" always overrides. Editing the text
clears the schedule the old text chose.

**Data first, decisions second.** A rule's wake-up and the source poll it depends on are both
anchored to the hour, so an evaluation waits for any collection that is running or overdue
before it reads the world (up to `WARDEN_COLLECT_WAIT_SEC`, default 180s). Without that
barrier a rule reads the *previous* run's stored data and acts a full cadence late.

To reason about school days the evaluator gets a day-by-day lookahead from Weduc's own
calendar (`school.upcoming_days`, `school.next_school_day`) covering term dates, INSET days and
closures — a lookup, not an inference, because a weekday in term time can still be an INSET day
and Weduc's term *periods* run past the last school day.

## Quick actions

`quick_actions:` in `config/warden.yaml` defines the Dashboard's one-tap buttons — a step naming
only a `service` fans out to every group that has it ("everywhere"), a step naming a `group`
covers its whole service list. `revert_after_minutes` snapshots just the affected switches and
restores them later, so a movie-night unblock can't be left open overnight. Adding a button is a
config edit, not a code change.

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
`learning-journey-targets` (this week's plan), `score` (attainment 0–1 overall + per subject
+ mastery, with monthly history) and `mock_tests` (exact % correct) — assembling a **state of play** the dynamic rules reason over
(today's minutes, per-topic scores, subject attainment, recent mock results, assignments). Each
change is archived (`GET /api/sources/atom/history`) so Luke's progress accumulates over time.

The week's workload comes from Atom itself: `learning-journey-targets` gives this ISO week's
islands per subject for Luke's year group and course, plus any mock test set for the week —
the very number his dashboard counts against, and it varies every week (17 one week, 20 + a
mock the next). A mock test counts as one item of the plan, as Atom counts it. Warden derives
the daily share from that total: what's left of the week spread over the days the week has
left, rounded up — published as `atom.luke.islands_due_today` and used as the bar for
`atom.luke.daily_complete`. Miss a day and the days after it get harder; finish early and
they get easier. You can override a single week on the Sources card (`PUT /api/sources/atom/target`,
`DELETE` to hand it back to Atom's plan); the config value is only the fallback for a run
that could not read the plan.
Until you capture a session, Sources → **Fixtures** runs a realistic offline sample.

## Weduc / ReachMoreParents — first run (username + password)

Weduc is a plain form login, so unlike Atom **no human is needed**: put
`WEDUC_USERNAME` / `WEDUC_PASSWORD` in `.env` and the adapter captures its own session
(and silently re-captures whenever it expires).

```powershell
cd warden
.venv\Scripts\python.exe -m warden.adapters.weduc.login          # capture now
.venv\Scripts\python.exe -m warden.adapters.weduc.login --recon  # + map the API
```

Two things about this portal are easy to get wrong and worth knowing:

- It is a **Blazor WebAssembly** app served from `ui.app.weduc.co.uk`, but its REST API
  is a **different host**, `app.weduc.co.uk` (the SPA reads that from its own
  `/appsettings.json`). Asking the UI host for an API path returns the SPA shell with
  HTTP 200 — so "it 200s" is not proof the session works. The adapter checks for JSON.
- Auth is a plain `PHPSESSID` cookie, no bearer token, so the captured jar replays from
  httpx headlessly — no browser at collect time, container included.

Live endpoints used: outstanding forms, newsfeed river, messages, child profile, calendar
events and the newsletter PDFs. The forms list drives `weduc.<subject>.forms_outstanding`;
the calendar drives `weduc.<subject>.school_day_today` and the school-day lookahead. Meals
are **not yet collected** — `--recon` is the tool for mapping them.

### Reading the newsletter (`warden/documents.py`)

Attachments hang off `postType.items[]`, not an `attachments` key. The adapter stays
mechanical — it downloads and caches the file, keyed on the portal's **file id** (the hash
in the download URL rotates per session) — and the hub reads it.

Reading sends the extracted text **plus a rendered image of every page**: on a real
newsletter the text layer contained "Swordfish" but "Merit" appeared zero times, because
the merit list is a picture. The 27.5 MB source PDF is too large to send whole; six pages
at 140 dpi come to ~2.2 MB.

Because vision models misread names off graphics — one run reported `"HETE-ROSE"` as the
child winning an award — every claimed mention is **quote-verified in code** against the
child's name before it counts. Rejects are kept in `unverified_child_mentions` rather than
dropped, and `child_mentioned` is recomputed from survivors. Digests cache by file id, so
each newsletter is read once. Needs `ANTHROPIC_API_KEY` (images); skipped with a note
otherwise.

### Daily reminders (`warden/reminders.py`)

The Weduc adapter brings back the class calendar, the head's messages, forms and term
dates — but as ~140 undifferentiated Items. The **Reminders** tab answers the question
that data is actually for: *is there school tomorrow, what does he take, and is there
anything I was meant to have done?*

Two layers, and the split is the point:

- A **deterministic spine** that needs no API key: school/closure days from the adapter's
  term context, dated events from the calendar, outstanding forms, newsletter key dates.
  With the LLM off the page is thinner but still correct.
- An **LLM pass** over recent messages, because that is where the un-mechanical value is:
  *"please can each child bring in a plastic carrier bag tomorrow"* is a Thursday reminder
  only if you know it was sent on the Wednesday. Each message is given to the model with
  its own sent date so relative days resolve correctly.

Same posture as the newsletter reader: **the model proposes, code verifies.** A reminder
is shown only if it cites a message that was actually in the input, quotes words that
really appear in it, resolves to a date not already past, and is not addressed to another
class. Rejects are kept in `unverified`, not silently dropped — an invented "bring £5 on
Friday" would cost the page its credibility. Readings cache in the kv store under a hash
of the messages, so a re-poll that changed nothing costs nothing.

Class relevance is data-driven: the class names come from the audience tags the portal
itself puts on posts, so another class's swimming slot stays off the page without any
hard-coded roster.

## ParentPay — clubs, with no second password (`warden/adapters/parentpay/`)

Breakfast club and after-school club are **not** booked in Weduc. Weduc's Payments page
is a redirect: it mints a one-shot token and bounces you to
`app.parentpay.com/...#/partnerlogin?token=…`. So Warden reaches ParentPay using the
Weduc credentials it already has — **there is no ParentPay secret to configure**.

The token rides in the URL *fragment*, which is never sent to a server, so only the
ParentPay SPA can exchange it. That is the one step needing a browser; the session it
mints is then replayed headlessly with httpx. If the Weduc session has expired, that is
refreshed first (it logs in unattended), so the whole chain self-heals.

The bookmarkable `ClubsCalendar.aspx` page is an Angular shell — fetching its HTML yields
nothing. The adapter uses the API behind it instead:

```
GET /clubsbookingApi/internal-details/{club_id}/{consumer_id}   -> internalMemberId
GET /clubsbookingApi/club-booking-calendar/{club_id}/{member}   -> sessions[] + cut-off
```

Each session gives `status` (Booked / Available / Unavailable), capacity and current
count. **The cut-off is the point**: `bookingCutOffSettings` is structured data, and the
two clubs here differ — breakfast closes 09:00 the day *before*, after-school closes 09:00
*on the day*. So the adapter computes a real deadline per session instead of repeating one
sentence, and an unrecognised scheme yields no deadline rather than a guessed one.

It also reads the school's **payment items** — trips, swimming, residentials — from
`/V3Payer4W3/Home/PaymentItems/PaymentItems.aspx`. That page is classic server-rendered
ASP.NET with no JSON behind it, so it is the one place this adapter parses HTML; it
anchors on the repeater's element ids (`rptPaymentItems_PaymentItem_N`), which come from
the server control, rather than on the Bootstrap classes around them.

The distinction that matters is **charge vs account**. A row showing `Payment due` and a
money amount is a one-off somebody must settle; a row showing `Balance` is a running
account (school meals, the clubs) that tops up and is never a task. Only the first kind
can be outstanding, so only it becomes an action — otherwise every visit to the page
would demand you "pay" a meal account that is simply in credit. A charge showing `Paid`
is done. ParentPay's own `New!` flag is carried through.

Strictly read-only: GETs only. The same API can book, cancel and pay; this code never
calls those endpoints.

## School lunches

The Weduc adapter also reads `POST /rest/dinner/getEvents`, which returns a row per day
with the actual choice (`selections: ["Fish Fingers"]`) and a state colour the meals page's
own legend defines. A school day with **no row at all** is what "nothing ordered" looks
like — so the gap is found by joining the meal rows onto the term calendar, not by
expecting a row that says so.

## Add a data source (the whole point of "pluggable")

1. Create `warden/adapters/<name>/adapter.py` with `class Adapter(SourceAdapter)`
   implementing `authenticate` / `collect` / `healthcheck`, emitting `Item[]` and any
   `Signal[]` it can derive.
2. Add a `sources:` block and any new `signals:` to `config/warden.yaml`.
3. Reference the new signal in a plain-English rule. Done — no core changes.

## Endpoints

`GET /api/state · /status · /config · /audit` · `POST /api/actions/{service,group,client,protection}` ·
`/api/rules` CRUD + `/preview` + `/{id}/run` · `/api/sources` + `/{key}/run` + `/{key}/items` ·
`/api/signals` + `/emit` · `GET /api/reminders` + `POST /api/reminders/refresh` ·
`GET /ws` (live). Full contract in `INTERFACES.md`.

## Security & scope

Everything here controls **your own network and your own kids' devices**. Scrapers log
into **your own accounts** (read-only) with credentials from your `.env`. No third-party
targeting, no evasion of anyone else's controls. Rotate `ADGUARD_PASS` if it has ever been
pasted into a chat.
