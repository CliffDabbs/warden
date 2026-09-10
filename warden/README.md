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
| The multi-source **digest platform** contract (`../docs/sourceadapterCONTRACT.md`) | Warden's **pluggable source adapters** + **signal bus** |
| HA automations / schedules | Warden's **plain-English rules engine** (cron + signal triggers) |

## Architecture

```
 Web UI (no-build SPA)  ── /api + /ws ──►  FastAPI
        │                                    │
        │              ┌─────────────────────┼───────────────────────┐
        ▼              ▼                     ▼                        ▼
   Dashboard     AdGuard service      Rules engine            Source registry
   Reminders     (live + fake)        + scheduler              (pluggable)
   Devices        clients/services    ⏰ cron + signal          ┌ atom      (Google SSO,
   Rules          rules/protection    🧠 LLM evaluator         │            captured once)
   Sources                             (self-scheduling)       ├ weduc     (form login,
   Activity      Host control                                  │            self-recapturing)
   About         (SSH → QNAP)                                  └ parentpay (SSO off Weduc)
                       │                     ▲                        │ emits
                       ▼                     │ reacts                 ▼
                AdGuard Home @10.7.11.29   Signal bus ◄──────────  Signals
                Plex on the NAS @.21       (latest + history)     SQLite (state + history)
```

- **Sources are pluggable.** Adding one (e.g. a rugby-club WhatsApp export) means writing
  one adapter to `warden/adapters/base.py` and adding a block to `config/warden.yaml` —
  the core never changes. `atom`, `weduc` and `parentpay` ship as examples.
- **Signals are the bridge.** An adapter emits typed facts like
  `atom.luke.daily_complete = true`; rules react to them.
- **Runs anywhere.** With no AdGuard reachable it falls back to an in-memory *fake*
  (set `ADGUARD_MODE=live` to remove the fallback so failures surface instead); with no
  Anthropic key *and* no `claude` CLI the rule compiler falls back to a deterministic parser; and every source ships
  fixtures you can run on demand. So it always boots and demos. What it will *not* do is
  quietly substitute: a **live** run that cannot reach its portal fails and keeps the
  previous data, rather than passing fixtures off as real.

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

One file defines your **groups** (kids/tv, each claiming AdGuard clients by tag — tag a
device `user_child` and it joins the kids group with no config entry of its own),
**clients** (only the devices you want tied to a named person), **services** (the on/off
toggles), **subjects** (people), **sources** (scrapers + cron), **quick_actions** (the
Dashboard's buttons), **hosts** / **managed_services** (the SSH lever), and the **signal
vocabulary** the rule compiler understands.

It is read **once at startup** — edit it and restart (`docker compose restart warden`).
Secrets never live here; they come from `.env` via the names in each source's `secrets:`
map.

## How a rule works

1. You type English in the **Rules** tab. Warden shows a live compiled preview.
2. On save it's compiled to a `CompiledRule` (trigger + conditions + actions) and stored.
3. **Schedule** triggers ("at 8pm every day") run on a cron scheduler.
   **Signal** triggers fire when the signal bus sees the matching transition.
4. Actions: allow/block a service for a group, disable a whole group ("all kids
   devices"), allow/block one named device, **start or stop a whole-house service over
   SSH**, toggle AdGuard protection, or add/remove a custom filtering rule.
5. Every action is written to the **Activity** log and pushed live to the UI.

## Two kinds of rule — deterministic and AI-evaluated

Warden splits rules by whether they depend on live data:

- **Deterministic (⏰)** — pure time/action rules like *"at 8pm every day, disable all kids
  devices"* run on an exact cron schedule. No LLM, no cost, precise to the minute.
- **Dynamic (🧠)** — rules that reference someone's live progress (*"switch on YouTube when
  Luke has done 30 mins and scored 80%"*) are **evaluated on the fly by the LLM**. Each cycle
  Warden hands the model (a) the rule, (b) the live **state of play** from the sources (Atom:
  today's minutes, topics, assignments…), and (c) the **menu of switches** with their current
  state — and it returns which switches to flip, *from that menu only*. The adapters do still
  publish a baked `atom.luke.daily_complete` signal, but a dynamic rule doesn't consume it —
  the evaluator re-reads the raw state of play and the rule's own English each time, so a rule
  can set a bar the signal doesn't encode ("30 mins *and* 80%").

Decisions are **validated** (only real groups, services and host services get through) and
**audited with the reasoning**, which also shows on the rule card (*"last: Luke has done 0 min
today; the rule requires 30"*). They are **idempotent by instruction**: the model is shown each
switch's current state and told not to return a no-op — the write path doesn't re-check, so
that one is a prompt guarantee rather than a code one.

Dynamic rules need an LLM backend (API key or the Claude CLI); with neither they are skipped.
The compiler auto-classifies: mention a person or words like minutes/score/done/practice and a
rule becomes dynamic; otherwise it stays deterministic. **The classifier promotes but never
demotes** — so a rule can compile to a perfectly good signal trigger and *still* be marked
dynamic, at which point that trigger is decorative and only the evaluator acts on it. The
seeded "when Luke has completed his Atom Learning" rule is exactly this case, which is why it
does nothing at all without a backend.

**It refuses to act on stale data.** If any enabled source's stored data isn't live — an
expired session, a portal that didn't answer — every dynamic rule is skipped for that cycle,
switches are left where they are, and Activity records *"refusing to act on non-live data"*.
Better to leave the house as it is than to act on last week's picture of it.

### Dynamic rules schedule themselves

There is **no fixed polling interval**. A rule's English carries its own cadence, and every
evaluation returns a `next_check_at` that the engine arms as a one-shot job:

> "…No need to check while he's at school; on school days don't start checking until 4pm,
> then check hourly until 7pm or until he's finished. At 8pm block all streaming again."

While that instant is in the future the rule is **asleep** — the safety-net tick
(`WARDEN_EVAL_INTERVAL_MIN`, cron-anchored so checks land on the clock) won't wake it, and
nor will a poll that brings back the same facts as the last one. That gating is what makes
"don't check while he's at school" real rather than advisory.

New data can pull a rule forward, but **only if it was about to look anyway** — within a
two-hour early-wake window. A rule waiting on the data, checking hourly with its next look
minutes away, shouldn't sit on the answer to its own question; a rule that said "not until
tomorrow at 4pm" has already answered it, and waking it contradicts the instruction in its own
text. That distinction is what `scripts/test_wake_semantics.py` exists to hold.
Wake-ups are clamped to 5 minutes … 7 days,
persisted, and re-armed after a restart; "Evaluate now" always overrides. Editing the text
clears the schedule the old text chose.

**Data first, decisions second.** A rule's wake-up and the source poll it depends on are both
anchored to the hour, so an evaluation waits for any collection that is running or overdue
before it reads the world (up to `WARDEN_COLLECT_WAIT_SEC`, default 180s, **per source**). Without that
barrier a rule reads the *previous* run's stored data and acts a full cadence late.

To reason about school days the evaluator gets a day-by-day lookahead from Weduc's own
calendar (`school.upcoming_days`, `school.next_school_day`) covering term dates, INSET days and
closures — a lookup, not an inference, because a weekday in term time can still be an INSET day
and Weduc's term *periods* run past the last school day.

## Quick actions

`quick_actions:` in `config/warden.yaml` defines the Dashboard's one-tap buttons. A step naming
only a `service` fans out to every group that has it ("everywhere"); add `groups:` to narrow it;
a step naming a `group` covers its whole service list; a step naming a `host_service` starts or
stops one of the whole-house services below. `confirm: true` puts a dialog in front, `style:`
colours the button.

`revert_after_minutes` snapshots just the affected switches and restores them later, so a
movie-night unblock can't be left open overnight. That timer is held **in memory only** — if
Warden restarts before it fires, it never fires, on the reasoning that you noticing beats a
stale revert landing at random hours later.

Adding a button is a config edit, not a code change.

## Whole-house services — the lever DNS can't pull

Every switch above works by DNS filtering, which stops a device **looking up a name**. It
cannot stop a device that already knows an address — and a Plex client on your own LAN finds
the server by broadcast and connects straight to its IP, never asking DNS at all.

So Warden has a second, blunter lever. `hosts:` and `managed_services:` in
`config/warden.yaml` describe machines it can reach over SSH and the commands that start,
stop and query a service on them:

```yaml
hosts:
  qnap: { address: 10.7.11.21, port: 22, user_env: QNAP_SSH_USER, password_env: QNAP_SSH_PASS }

managed_services:
  - id: plex_server
    name: "Plex server"
    host: qnap
    confirm: true
    start:  "/sbin/qpkg_cli --start PlexMediaServer"
    stop:   "/sbin/qpkg_cli --stop PlexMediaServer"
    status: "/sbin/qpkg_cli --status PlexMediaServer"
    status_running_match: "running"
```

These appear as a **Whole-house services** bar on the Dashboard, and rules can name them
(`set_host_service`). They are deliberately kept apart from the per-group toggles, because
**the blast radius is the whole house**: a stopped service is stopped for everyone, and
anything mid-stream ends. Hence `confirm: true`.

Needs `QNAP_SSH_USER` / `QNAP_SSH_PASS` in `.env`; without them the card reads `?` rather
than failing. If the commands are wrong for your NAS — Container Station rather than a QPKG,
say — `GET /api/hosts/qnap/discover` reports what's actually there so you can swap them in.

## Manual holds — a flip by hand outranks the robots

Toggle a switch, press a quick action, or start/stop a whole-house service, and that exact
control is **held**. Dynamic rules and signal-fired rules then leave it alone — a "not on
track" verdict spares held switches and says so in Activity (*"left alone (set by hand…)"*) —
and the Dashboard badges them ✋.

The hold ends when a **scheduled** rule writes the switch (the 8pm switch-off is the daily
reset), when you press ▶ Run now, or at 4am as a fallback — whichever comes first. So
"streaming on" at 2pm means *on until 8pm*, not *on until the next hourly check*.

The point is that the automation is advisory between resets and the person in the room isn't
overruled by a model thirty seconds later.

## Device overrides

The **Devices** tab lists every client AdGuard knows about, and unblocks one for 2h / 4h /
24h / forever without touching its group. An override exempts that device from *group-level*
writes, so the 8pm rule can fire and still leave the one unblocked tablet alone; a sweep puts
timed ones back when they lapse. A group switch that is on for some devices and off for
others reads **mixed · blocked on N devices**.

## Turning protection off

The Network-protection toggle is the nuclear switch: AdGuard filters nothing, for everyone.
It stays available, because filtering does occasionally break something real — but turning it
off **asks for a reason and a name, both required**. The reason is refused if it's missing,
under 8 characters, or reads like a streaming excuse ("the kids want Netflix") — per-service
switches exist for exactly that, and the refusal names the word that tripped it.

**Refused attempts are logged too**, so probing the gate is visible. While protection is off
the whole app shows a lockout screen naming who turned it off, why, and when. This is
accountability, not access control; if you need the latter, set `WARDEN_PASSWORD`.

## Seeing what the model was asked

Warden asks a model to do four things — compile a rule, judge a dynamic rule, read the school
messages for Reminders, and read the newsletter. Each is a decision made in your house on a
model's say-so, so each is recorded **verbatim**: system prompt, the message as it went out,
and the reply as it came back.

Every call puts an `llm_call` line in Activity with its purpose, model, tokens and duration,
and a **prompt ⤢** button that opens the exchange. Failures are kept too — timeouts, replies
that were all thinking and no answer, malformed JSON — because those are the ones worth
reading. A retry after bad JSON is its own line, marked *(retry)*, rather than hiding behind
the answer that eventually worked. The last few hundred exchanges are kept; `GET /api/llm`
and `/api/llm/{id}` are the same thing over the API.

## Sign-in

Set `WARDEN_PASSWORD` (and optionally `WARDEN_USERNAME`, default `admin`) in `.env` to put a
login in front of everything — the UI, the API, and the live WebSocket. It's a form login and a
signed session cookie, not HTTP Basic; the cookie lasts ~10 years, so you sign in **once per
device**. `/healthz` and `/static/` stay public so the container healthcheck and assets still
work. Leave the password empty for open access (e.g. behind a VPN). "Log out" lives on the
About tab.

The signing key is derived from the credentials unless you pin `WARDEN_SECRET`, so changing the
password signs every device out — set `WARDEN_SECRET` to a random string if you'd rather it
didn't.

## Rule-compiler backend — API key *or* your Claude Code login

`WARDEN_LLM` picks how English becomes rules:

| value | uses |
|---|---|
| `auto` (default) | API key if set, else the Claude Code CLI if installed, else the parser |
| `api` | Anthropic API (`ANTHROPIC_API_KEY`) |
| `cli` | the local **`claude`** CLI (`claude -p`) — reuses your Claude Code login, **no API key, no extra billing** |
| `off` | the built-in deterministic parser only |

`WARDEN_LLM_MODEL` picks the model that compiles rules (haiku by default — cheap and fast).
`WARDEN_EVAL_MODEL` picks the one that judges dynamic rules, and is deliberately stronger,
because that model also has to do the clock-and-term arithmetic to choose its own next
wake-up.

The deterministic parser handles the common time-and-service phrasings offline, but it can only
ever emit `set_service` and `set_group` — per-device actions, whole-house service control,
multi-moment rules and every AI-evaluated rule need a backend. The active backend is shown on
the About tab.

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
+ mastery) and `mock_tests` (exact % correct) — assembling a **state of play** the dynamic
rules reason over (today's minutes, per-topic completion times, subject attainment, recent
mock results, assignments). Each
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
events, school meals (`POST /rest/dinner/getEvents`) and the newsletter PDFs. The forms list
drives `weduc.<subject>.forms_outstanding`; the calendar drives
`weduc.<subject>.school_day_today` and the school-day lookahead; the meals join drives
`weduc.<subject>.lunch_booked_tomorrow`. When the portal changes and an endpoint stops
answering, `--recon` is the tool for re-mapping it.

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
dates — but as a couple of hundred undifferentiated Items. The **Reminders** tab answers the question
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

Each adapter also carries a `manifest.json` (id, capabilities, `secrets_required`,
`emits_signals`) parsed at import — a bad one fails discovery — and a `fixtures/` folder so it
can still run offline. `collect()` returns `Item[]`, `Signal[]` and a free-form `state` dict:
that last one is the "state of play" the AI-evaluated rules actually reason over, so put the
structured facts there rather than only in prose items.

**Not yet integrated: Amazon Kids tablets.** `warden/adapters/amazon/` holds a recon tool
(`python -m warden.adapters.amazon.login --check`) and nothing else — no adapter, no manifest,
no config entry, so naming it in `config/warden.yaml` would fail. The gap it's aimed at is
real and unreachable by DNS: a Fire tablet playing a film it has already downloaded. See
`../docs/amazon-parent-dashboard-RECON.md`.

## Checking it still works

```powershell
.venv\Scripts\python.exe scripts/test_wake_semantics.py   # offline; guards the wake-up rules
```

`scripts/smoke.py` exercises the API end to end, but **it mutates whatever it points at** —
its last step sets `kids/youtube = allowed` and never restores it, and the Atom run it does
(against fixtures, `live=false`) still pushes signals onto the live bus, which can make dynamic
rules fire. Point it only at a throwaway instance:

```powershell
$env:ADGUARD_MODE="fake"; $env:WARDEN_PORT="8099"; $env:WARDEN_DB="data/smoke.db"
.venv\Scripts\python.exe -m warden                          # separate terminal
.venv\Scripts\python.exe scripts\smoke.py http://127.0.0.1:8099
```

## Endpoints

| Area | Routes |
| --- | --- |
| State | `GET /api/state` · `/status` · `/config` · `/audit` · `/llm` · `/llm/{id}` |
| Actions | `POST /api/actions/{service,group,client,protection}` · `GET /api/actions/quick` · `POST /api/actions/quick/{id}` |
| Devices | `GET /api/devices` · `POST /api/devices/unblock` · `DELETE /api/devices/override` |
| Hosts | `GET /api/hosts/services` · `POST /api/hosts/services/{id}` · `GET /api/hosts/{host}/discover` |
| Rules | `GET/POST /api/rules` · `PUT/DELETE /api/rules/{id}` · `POST /api/rules/preview` · `/{id}/run` · `/{id}/recompile` · `/{id}/toggle` |
| Sources | `GET /api/sources` · `POST /{key}/run` · `GET /{key}/items` · `/{key}/history` · `/{key}/documents` · `GET/PUT/DELETE /{key}/target` |
| Signals | `GET /api/signals` · `/{key}/history` · `POST /api/signals/emit` |
| Reminders | `GET /api/reminders` · `POST /api/reminders/refresh` |
| AdGuard | `GET/POST/DELETE /api/adguard/rules` |
| Other | `GET /ws` (live) · `GET /healthz` · `GET/POST /login` · `GET /logout` |

Full contract in `INTERFACES.md`.

## Security & scope

Everything here controls **your own network and your own kids' devices**. Scrapers log
into **your own accounts** (read-only) with credentials from your `.env`. No third-party
targeting, no evasion of anyone else's controls. Rotate `ADGUARD_PASS` if it has ever been
pasted into a chat.
