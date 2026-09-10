# Warden — User Guide

Warden is the control panel for your home's parental controls. You write rules in plain
English; it drives AdGuard Home directly and reads your kids' school and learning
portals to decide when those rules apply.

> "At 8pm every day, disable all kids devices."
> "When Luke has completed his Atom Learning, allow YouTube for the kids."

This guide covers running it day to day. For the design and the source-adapter contract,
see [README.md](README.md) and [INTERFACES.md](INTERFACES.md).

---

## 1. Starting and stopping

```powershell
cd <your warden checkout>\warden
.venv\Scripts\python.exe -m warden
```

Then open **<http://localhost:8080>**.

To run it detached (and keep a log):

```powershell
Start-Process -FilePath ".\.venv\Scripts\python.exe" -ArgumentList "-m","warden" `
  -WindowStyle Hidden -RedirectStandardOutput "data\server.log" `
  -RedirectStandardError "data\server.err"
```

To stop it:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*warden*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

`run-warden.cmd` does the same thing and is what a Scheduled Task should point at. It logs
to `data\warden.log` rather than the two files above.

In a container instead:

```bash
docker compose up -d --build     # start
docker compose logs -f warden    # watch
docker compose down              # stop
```

**Health check:** `curl http://localhost:8080/healthz` → `{"ok":true,"adguard_mode":"live"}`.
If `adguard_mode` says `fake`, see §7.

---

## 2. The tabs

| Tab | What it's for |
| --- | --- |
| **Dashboard** | The switches. Toggle a service (YouTube, Netflix…) per group, or a whole group at once. Changes hit AdGuard immediately. Above them, **Whole-house services** — see §7. |
| **Reminders** | The school day: is there school tomorrow, what does Luke need to take, what's he having for lunch, is he booked into clubs, which forms are outstanding. See §2a. |
| **Devices** | Every device AdGuard knows about. Unblock one for 2h/4h/24h/forever without touching its group. |
| **Rules** | Write, preview, enable/disable and delete rules. Shows each rule's compiled form and, for dynamic rules, the reason behind its last decision. |
| **Sources** | Atom, Weduc and ParentPay: last run, next run, health. **Run live** fetches for real; **Fixtures** runs the offline sample. The **Signals** panel at the bottom shows every signal's live value and lets you push one by hand. |
| **Activity** | Every action taken, who or what caused it, and why. This is where you look when something changed unexpectedly. |
| **About** | Which AdGuard mode and which LLM backend are active. "Log out" lives here. |

The dot next to the tabs is the live WebSocket — green *live*, grey *reconnecting*. Grey
doesn't mean stale: the page falls back to polling every five seconds and reconnects on its
own, so a reload is only worth it if it stays grey. (It's hidden on narrow screens.)

A switch can also read **mixed · blocked on N devices** — some devices in the group block it
and some don't, usually because one has a timed unblock from the Devices tab. The toggle is
drawn *off* in that state deliberately, so one tap means "allow it everywhere".

---

## 2a. Reminders — the school page

The morning question, answered from the school portal: **is there school, what does he
take, and was I supposed to have done something?**

At the top, today and tomorrow: school or no school, and why (weekend / school holiday /
INSET). Then anything genuinely missing, then outstanding forms, oldest deadline first,
with overdue ones in red. Then a card per day for the week — days with nothing on are left
out, except today and tomorrow, because "nothing to bring" is itself the answer. *Show
more days* opens the rest of the fortnight.

### Clubs and lunch

Each school day lists what Luke is having for lunch, and whether he is booked into
breakfast club and after-school club.

The lunches come from Weduc. The club bookings come from **ParentPay** — which is where
the school actually takes them; the Payments section of ReachMoreParents is just a
redirect to it. You do not configure a ParentPay password: Warden signs in through the
same partner-login handoff you use, on the Weduc credentials already in `.env`.

**Clubs are shown one way only: a green `Booked` against the day.** A day he is not booked
into simply has no club row. That is deliberate — clubs get booked a week or two at a
time, so most future days are legitimately empty, and listing them turned the page into a
standing to-do list of things that were fine. The question the page answers is "is he
in?", and for an empty day the honest answer is the absence of a row.

Links straight to each club's ParentPay calendar sit in the footer, for when you *do* want
to book something.

**Money the school wants** appears in a **To pay** card near the top: trips, swimming,
residentials — anything ParentPay is asking for, with the amount, the due date and a link
straight to it. Items ParentPay has just raised are tagged `New`, and anything past its
due date turns red.

Running balances are deliberately *not* shown there. Your school-meals account and the two
club accounts top themselves up rather than being settled once, so listing them would put
a permanent "you owe money" card on the page when you don't.

**Lunches are the exception**, because a missing one is a real gap with nothing else to
catch it. Each day shows the meal with a green `Booked`, and a school day with nothing
ordered raises a red **Nothing ordered** card at the top. That is detected by the absence
of a booking on a day the term calendar says is a school day, so weekends and INSET days
never raise it.

Warden also drops the school's own "order your dinners" messages from the page whenever
every school day is already booked — the booking data has answered them, and repeating the
nudge would send you to redo a job that is done. It comes straight back if a real gap
appears.

Clubs are polled at 07:00 and 18:00. Worth knowing if you ever go looking: the two clubs
have *different* booking cut-offs — breakfast club closes at **09:00 the day before** a
session, after-school club at **09:00 on the day** itself.

**Where each line came from is on the line.** A grey footnote says *school calendar*,
*term dates*, or `from "Plastic Bag for tomorrow" · 2026-07-15`. Hover it to see the exact
sentence the reminder was read from. If a reminder ever looks wrong, that is how you
check it in seconds.

**The "inferred" badge matters.** Most reminders come from a message that names its day
outright. `inferred` means the message said something like *"bring it in tomorrow"* and
Warden worked out the date from when the message was sent. Those are the ones worth a
second glance.

Below the days, anything found with no fixed date sits under **No fixed date / still
outstanding** rather than being guessed onto a day. Last, just above the footer, a **📰 Latest
newsletter** card carries the digest of the most recent one, with an age badge and a *names
Luke* badge when he's actually in it.

Reminders are built from data Weduc has **already** collected — the page never scrapes, so
it loads instantly. That includes the days a new message has just arrived: reading messages
takes the model 15–20 seconds, so the page never waits for it. Everything computed in plain
code — school days, lunches, clubs, forms, payments — is on screen straight away, a
*reading messages…* chip appears top right, and anything found in the messages drops in by
itself when the reading lands. (Before, the whole page sat on "Loading…" for those 20
seconds and showed nothing at all.)

Weduc is polled once a day at 07:00; if the banner says the data is stale, run the source
from **Sources → Weduc → Run live**. **Re-read messages** at the bottom re-runs the reading
instead of reusing the cached one — that button does wait, since you asked for it — and it
costs one LLM call, which is why it isn't done on every page view. A reading that fails is
remembered for ten minutes, so a broken model call can't loop.

Without any LLM backend at all — no `ANTHROPIC_API_KEY`, no `claude` CLI, or `WARDEN_LLM=off`
— the page still works: term dates, calendar events, lunches, clubs, payments and forms are
all computed in plain code. You lose only the reminders read out of message text, and the
footer says so. Note the split: **message reading works on either backend, but the newsletter
needs the API key specifically**, because it sends page images and the CLI path can't.

What it deliberately will **not** do is guess. A reminder is shown only if it quotes a
message that really exists, lands on a date that has not passed, and is not addressed to a
different class. Anything else is dropped rather than shown as a maybe.

---

## 3. Writing rules

Type plain English into **Rules**. You get a live compiled preview before saving.

Warden sorts every rule into one of two kinds, automatically:

**Deterministic (⏰)** — pure time-and-action. Runs on an exact cron schedule. No LLM, no
cost, precise to the minute.

> "At 8pm every day, disable all kids devices"
> "Block Netflix for the kids on school nights at 9pm"

**Dynamic (🧠)** — anything that depends on live data: someone's progress, minutes,
scores, outstanding work. These are judged by the LLM against the current state of play,
and it may only pick from the switches that actually exist.

> "Allow YouTube for the kids once Luke has done 30 minutes of Atom and no set work is outstanding"

A rule becomes dynamic if it mentions a person or words like *minutes, score, done,
practice, forms, homework*. You can see which kind you got in the preview.

### Dynamic rules schedule themselves

**There is no fixed polling interval for a dynamic rule.** Write *when* to check into the
rule alongside *what* to do, and each evaluation picks its own next wake-up:

> "Block all streaming on the TVs and kids' tablets until Luke is on track for his Atom
> learning. On track means Monday 1/7th complete, Tuesday 2/7ths, and so on. No need to
> check while he's at school; on school days don't start checking until 4pm, then check
> hourly until 7pm or until he's finished. At 8pm block all streaming again."

That one rule carries a condition, a progressive weekly target, and a whole checking
schedule. Warden asks the model for a `next_check_at` every time it evaluates, arms a
one-shot job for exactly that moment, and shows it on the rule card ("next check today
16:00") with the model's reasoning underneath.

What follows from that:

- **A future `next_check_at` means the rule is asleep, deliberately.** The clock doesn't
  wake it — not the safety-net tick, not a source poll that brings back the same facts as
  last time. That's what makes "no need to check while he's at school" actually hold.
- **New data can wake it — but only if it was about to look.** A collection that genuinely
  changes the state of play pulls a rule forward only when its own next check is already
  within **two hours**. A rule that said "don't check again until tomorrow at 4pm" stays
  asleep, because that instruction is the whole point of writing it. The early wake is for
  the rule that is *waiting* on the data — checking hourly, next look minutes away — which
  shouldn't sit on the answer to its own question.
- **Evaluations wait for the data.** A rule's wake-up and the hourly poll are both anchored
  to the hour, so before reading the world an evaluation waits for any collection that is
  running or overdue (up to `WARDEN_COLLECT_WAIT_SEC`, default 180s, **per source** — and
  sources collect one at a time, so three overdue at once is three times that). Otherwise the rule
  would judge the *previous* hour's data and act a cadence late.
- **A manual flip outranks the rules until the next scheduled reset.** Toggle a
  switch, hit a quick action, or start/stop a whole-house service and that exact
  control is **held**: dynamic rules and signal-fired rules leave it alone — a "not
  on track" verdict spares held switches and says so in Activity ("left alone (set
  by hand…)"), and the Dashboard badges them ✋ manual. The hold ends when a
  *scheduled* rule (or a rule you press ▶ Run now on) writes the switch — the 8pm
  switchoff is the daily reset — or at 4am as a fallback, whichever comes first. So
  "Streaming on" at 2pm now really means *on until 8pm*, not *on until the next
  hourly check*. (Per-device flips use the device-override system instead, which
  even schedules respect.)
- **"Evaluate now" always overrides it.** Use that to test, or when you've changed
  something and don't want to wait. It also waits for any collection that is running or
  overdue before judging — but it won't re-scrape data that is already current.
- **Stale data stops everything.** If any enabled source's stored data isn't live — an
  expired session, a portal that didn't answer — *every* dynamic rule is skipped for that
  cycle, not just the ones that read that source. Switches are left exactly where they are
  and Activity records *"refusing to act on non-live data"*. The Sources card shows a red
  banner while that holds. Leaving the house as it is beats acting on last week's picture
  of it.

Editing a rule's text clears its schedule, because the text is what chose it.

Guard rails: a wake-up is clamped to **no sooner than 5 minutes and no later than 7 days**,
and if the model returns nothing usable it falls back to an hour. When a clamp bites, the
rule card says so — e.g. asking to sleep until September during the summer holiday shows
*"(clamped: asked to wake >7d out)"*, and the rule re-checks weekly instead.

### School awareness

Because the Weduc adapter reads the school's own calendar, dynamic rules know about term
dates, INSET days and closures. The world state carries a day-by-day lookahead
(`school.upcoming_days`) plus `school.next_school_day`, so "is Wednesday a school day?"
is a lookup rather than a guess. A rule saying "only on school days" behaves correctly
through half terms and INSET days without you maintaining a list of dates.

Note that a term *period* in Weduc runs to the end of its month — Summer Term is listed
as ending 31 August even though the children break up in July — so Warden derives the real
boundaries from the calendar's "End of Term" / "Term starts" events instead.

### Read the warnings

The compiler tells you what it assumed, and those notes are worth reading before you save.
The ones it emits:

- *"no time given; assuming 00:00"* — you said "every day" without an hour.
- *"no group named; defaulting to 'kids'"* — you said "block YouTube" without saying for whom.
- *"no schedule or signal trigger recognised; rule will only run manually"* — it saved, but
  nothing will ever fire it. Usually a rephrase, not a bug in your intent.
- *"this covers N separate moments…"* — see below.
- *"evaluated on-the-fly by the LLM against live source data"* — the rule was judged dynamic
  and the parser found no fixed allow/block in it, so there is nothing to preview.
- *"the model could not compile this (…) — the built-in parser did instead"* — see below.

The judgement the compiler **cannot** make for you is the one that bites most often: a rule
that only ever *opens* a gate will leave it open. "Let the kids have Roblox on Saturday
mornings until 11" allows within the window and never blocks outside it, so it needs a second
rule to close it again. Nothing warns you about that; check it yourself.

### When a compile goes wrong

Two failure modes, both visible on the card:

- **Several moments in one sentence** ("at 8pm block…, at 9.15pm unblock…") can't become one
  cron trigger, so it stays as a single AI-evaluated rule with a warning saying how many
  moments it covers. That works, but it costs an LLM call per check where two separate
  deterministic rules would cost nothing.
- **The model failed** — timeout, bad JSON, no backend. Warden falls back to the built-in
  parser, which may well have understood less of your sentence, so it stamps a warning on the
  rule and **caps its confidence at 0.5**. That cap is the tell: a rule showing 50% when you
  expected more probably compiled down to a fraction of what you asked for.

A **Recompile** button appears on any rule carrying an error or a warning. It re-runs the
compile on the stored text unchanged — useful once you've fixed a missing API key.

### Testing a rule safely

**The buttons in the UI apply for real.** Both "▶ Run now" (deterministic) and "Evaluate
now" (dynamic) execute immediately and change AdGuard — there is no dry-run button.

The **Signals** panel at the bottom of Sources is the fastest safe-ish end-to-end test: it
shows every signal's live value and lets you push one by hand. Flip `atom.luke.daily_complete`
to true and the "allow YouTube" rule fires — for real, so do it knowingly.

Dry run is available on the API only, and it's worth using before you trust a new rule:

```powershell
curl.exe -s -X POST http://localhost:8080/api/rules/seed-3/run `
  -H "Content-Type: application/json" -d '{\"dry\":true}'
```

It reports the decision, the reasoning and the actions it *would* take, and changes
nothing. Swap `seed-3` for the rule's id (visible in `GET /api/rules`).

One caveat for AI rules: a dry run reads whatever is **already stored**. Unlike a real
evaluation it doesn't wait for an in-flight collection, and it doesn't apply the
refuse-on-stale-data guard — so it can show you a decision the real run would decline to
make. Check the Sources card is green before trusting it.

---

## 4. Sources and signing in

Only one portal needs a username and password: **Weduc**. Atom uses a Google session you
capture once by hand, and ParentPay signs in off the Weduc session with no secret of its own.
All secrets live in `.env` — never in `config/warden.yaml`.

### Atom Learning — sign in once, by hand

Atom uses Google, which blocks automated browsers. So you sign in yourself, once:

```powershell
.venv\Scripts\python.exe -m warden.adapters.atom.login
```

Your own Chrome — or Edge, whichever it finds — opens at Atom → sign in with Google → it
detects success and saves `data/atom_state.json`. From then on Warden calls Atom's API
headlessly with that session. Pass `--browser` if it can't find either.

**You shouldn't need to repeat this often.** Atom's session cookie slides forward every
time it's used, and Warden writes the refreshed one back after each poll — so as long as
it keeps polling, the session stays alive. Re-run the command if Sources says the session
expired (e.g. after a long shutdown).

### The weekly island target — Atom sets it, Warden paces it

The number of islands Luke's week is supposed to contain changes every week, and **Atom
publishes it**: his learning journey sets a target per subject for the ISO week, plus any
mock test. Warden reads that each time it polls, so the figure on his Atom dashboard and
the figure Warden judges him against are the same number — nothing to copy across on a
Monday morning.

For example, the plan for w/c 7 Sep 2026 came to **21**: English 9 + a mock test, Verbal
Reasoning 6, Non-Verbal Reasoning 4, Science 1. A mock test counts as one item of the week
exactly as Atom counts it — it's why English showed */10* on his dashboard against a 9-island
target — so finishing one moves the week on by one.

Sources → Atom Learning → **Weekly islands** shows it, labelled *from Atom*, with the
breakdown on hover. Type over it and Save to **override just this week** (illness, half
term, a week worth pushing) — it then reads *set by you* and your number wins. **Use
Atom's number** puts the plan back in charge. Other weeks are untouched either way, and if
Atom can't be reached the last plan Warden read stays in force (the
`weekly_island_target` in `config/warden.yaml` is only the fallback behind that).

Next to it, Warden shows what today asks for — *today: 1/3*. That number is:

> whatever was still outstanding when today began, divided by the days the week has
> left (today included), rounded up.

Which means:

- **Change the weekly number and the daily one follows it.** 21 asks for 3 a day from a
  standing start on Monday; 28 asks for 4.
- **A missed day doesn't vanish** — it raises the days that follow. Nothing done by
  Thursday on a 21 week and the remaining four days ask for 6 each.
- **A week finished early asks for nothing more.** Once the target is met, today's
  requirement is 0 and the daily gate stops asking.
- **The bar doesn't move while he works.** It's measured from the start of the day, so
  finishing one island can't quietly lower the three he owes.

That figure is what "has Luke done his Atom learning today?" means — the
`atom.luke.daily_complete` signal every rule reads, and the reason line in Activity says
which number it was judged against. It's also published on its own as
`atom.luke.islands_due_today` if you want to name it in a rule.

**The island count isn't the only condition.** `daily_complete` also requires that no
parent-set work has been left unstarted, so it can read *not done* on a day the island target
was met. That outstanding count is published separately as
`atom.luke.assignments_outstanding`.

Saving an override — or clearing one — re-collects Atom straight away, so the new number
takes effect within seconds rather than at the next hourly poll. A rule can still define its own shape in
plain English ("Monday 1/7th complete, Tuesday 2/7ths…") — it reads the weekly total from
the same place and Warden's own split doesn't override it.

### Seeing exactly what the model was asked

Warden asks a model to do four things: compile a rule you typed, judge a dynamic rule
against the current state of play, read the school messages for the Reminders page, and read
the school newsletter. Each of those is a
decision made in your house on a model's say-so, so each one is recorded in full.

Every call puts a line in **Activity** — `llm_call`, with what it was for (`rule_eval:seed-3`,
`rule_compile`, `reminders`), the model, the tokens in and out, and how long it took. The
**prompt ⤢** button on that line opens the exchange:

- **System prompt** — the instructions the model was given.
- **Sent** — the message itself: the rule text, the world state, the whole thing, exactly
  as it went out.
- **Received** — the reply, exactly as it came back (a "copy" button on each).

Failed calls are kept too — a timeout, a reply that was all thinking and no answer,
malformed JSON — with whatever did come back and the error that ended it. Those are the
ones worth reading. A retry after bad JSON is its own line, marked *(retry)*, rather than
hiding behind the answer that eventually worked.

The header has an **LLM calls** checkbox if you want them out of the way; it's on by
default. Warden keeps the most recent few hundred exchanges — an evaluation prompt is
tens of thousands of characters, so older ones are trimmed, and their activity lines stay
even after the transcript behind them has gone.

### ParentPay — nothing to configure

There is no ParentPay password. Weduc's Payments page mints a one-shot token and bounces you
into ParentPay, and Warden rides that same handoff on the Weduc credentials it already has. If
the Weduc session has expired it refreshes that first, so the whole chain self-heals.

That handoff is the one step that needs a browser — the token travels in the URL *fragment*,
which never reaches a server, so only the ParentPay page itself can exchange it. The session it
mints is then replayed headlessly. It caches in `data/parentpay_state.json`; delete that file
to force a fresh handoff.

It is **strictly read-only** — GETs only. The same API can book, cancel and pay; this code
never calls those endpoints. Booking is a link out to ParentPay, in the Reminders footer.

Clubs and payment items are polled at 07:00 and 18:00.

### Weduc — automatic

Weduc uses a username and password, so there's nothing to do by hand. Put
`WEDUC_USERNAME` / `WEDUC_PASSWORD` in `.env` and Warden captures its own session —
and silently re-captures whenever it expires.

If Weduc ever turns on two-factor for your account, this breaks by design: Warden will
report that it can't refresh unattended rather than guessing.

### The newsletter PDFs

The weekly newsletter is where the genuinely personal content lives — a class's sports
result, a "Merit Book" naming individual children — and it appears **nowhere else** in the
portal. Warden downloads it, reads it, and puts what it found into the rules' world state.

Reading it needs care, for two reasons found the hard way on a real newsletter:

- **Text extraction alone silently misses the good bits.** The sample's text layer gave
  17k characters and did contain "Swordfish" and "Canada" — but "Merit" and "bell"
  appeared *zero* times, because those sections are pictures. So every page is rendered
  to an image and sent alongside the text.
- **Vision models misread names off graphics.** On that same newsletter the model reported
  `"HETE-ROSE"` as Luke receiving a Purple Star Award, and filed his class's merit list —
  which he isn't on — as a personal mention. A false "he won an award" is worse than no
  data, especially if a rule acts on it.

So every claimed mention is **quote-verified in code**: an entry survives only if its own
quote actually contains the child's name. Rejected claims aren't binned silently — they go
to `unverified_child_mentions` so you can see what was proposed and why it was dropped.
`child_mentioned` is then recomputed from what survived.

See it with:

```powershell
curl.exe -s http://localhost:8080/api/sources/weduc/documents
```

You get `summary`, `about_child` (verified only), `about_class`, `key_dates`,
`actions_for_parents` and `whole_school`.

Reading needs `ANTHROPIC_API_KEY` — it uses images, which the `claude` CLI path can't do.
Without a key it's skipped with a note rather than failing the run. Digests are cached by
the portal's file id, so each newsletter is downloaded and read exactly **once** however
often Warden polls. (The hash inside Weduc's download URL looks content-addressed but
rotates with the session — keying on it re-fetched 28 MB and re-billed a read every time
the session changed.) Requirements: `pymupdf`.

### When the portal changes — recon mode

Forms, newsfeed, messages, profile, the school calendar and school meals are all collected on
every run, and newsletter PDFs are downloaded and read. If Weduc changes and an endpoint stops
answering, re-map it:

```powershell
.venv\Scripts\python.exe -m warden.adapters.weduc.login --recon
```

That writes `data/weduc_recon.json` — every API call the portal makes, with response
shapes. Note the portal is a Blazor app whose UI and API live on **different hosts**, and
asking the UI host for an API path returns the page shell with HTTP 200 — so "it returned
200" is never proof a session works.

---

## 5. Quick actions

The Dashboard's one-tap buttons come entirely from `quick_actions:` in
`config/warden.yaml` — add an entry, restart, and it's a button. No code.

```yaml
quick_actions:
  - id: streaming_on_2h
    label: "Streaming on (2h)"
    description: "Unblock YouTube, Netflix and Disney+ everywhere, then put it all back"
    icon: play                  # play stop zap film shield clock youtube music gamepad
    style: good                 # default | good | warn | danger
    revert_after_minutes: 120
    steps:
      - { service: youtube,    state: allowed }
      - { service: netflix,    state: allowed }
      - { service: disneyplus, state: allowed }
```

Each step is one of:

| Step | Means |
| --- | --- |
| `{ service: youtube, state: allowed }` | every group that has YouTube — i.e. "everywhere" |
| `{ service: youtube, groups: [tv], state: allowed }` | only the listed groups |
| `{ group: kids, state: blocked }` | that group's entire managed service list |

`revert_after_minutes` snapshots **just the switches the action touches** and puts them
back afterwards — so "Streaming on (2h)" restores `kids/youtube` to blocked while leaving
`tv/youtube` allowed, rather than stamping a blanket baseline over everything. The revert
is held in memory: if Warden restarts before it fires, it doesn't happen, which is the
safer failure (you notice, instead of a stale revert firing at random later).

`confirm: true` puts a confirmation dialog in front — worth it for clamps and resets.

Quick actions create the same **manual holds** as individual toggles: the dynamic
rules won't undo them, the next scheduled rule (8pm) resets them, and a timed
revert (`revert_after_minutes`) releases the hold itself when it restores things —
unless you've re-flipped that switch by hand since, in which case your newer choice
wins and the revert leaves it alone.

## 6. Cadence and cost

- **Dynamic rules** wake themselves (§3). The `WARDEN_EVAL_INTERVAL_MIN` tick (currently
  **60** minutes, at half past) is only a safety net for rules with no wake-up armed.
- **Source refreshes** evaluate when they bring back something new — and even then only pull
  a rule forward if its next check was already inside two hours. Unchanged data wakes nothing. So the extra cost is one round of
  evaluations per real change — on a quiet day, none.
- **Deterministic rules** run on their cron and cost nothing.

Because rules sleep between their own chosen checks, cost tracks how often your rules ask
to be checked rather than a fixed poll. The worked example above is idle all morning, then
hourly from 4pm to 7pm — a handful of evaluations a day. Pennies a month.

Two models, deliberately: compiling a rule uses the cheap one (`WARDEN_LLM_MODEL`, haiku by
default), while *judging* a dynamic rule uses `WARDEN_EVAL_MODEL` (currently
`claude-sonnet-5`) on a prompt of roughly 20k tokens — because that call also has to do the
clock-and-term arithmetic to pick its own next wake-up, which is measurably harder than
compiling and worth the stronger model.

The eval tick is cron-anchored, so checks land at predictable wall-clock times and survive
a restart, rather than drifting from whenever the process happened to start.

---

## 7. AdGuard modes

`ADGUARD_MODE` in `.env`:

| Value | Behaviour |
| --- | --- |
| `auto` | Use the real AdGuard if **both** `ADGUARD_USER` and `ADGUARD_PASS` are set and `GET /control/status` succeeds; otherwise an in-memory fake. Default. |
| `live` | Insist on the real one. |
| `fake` | Never touch the network. Everything works; nothing is real. |

**If the About tab says `fake` when you expected `live`,** the usual cause is a wrong or
missing `ADGUARD_PASS` — or a blank `ADGUARD_USER`, which stops Warden trying at all. AdGuard
answers unauthenticated requests with HTTP 401, so Warden falls back rather than failing
loudly. Set `ADGUARD_MODE=live` if you would rather it failed loudly.

### What DNS blocking can and cannot do — the Plex case

Every switch here works by **DNS filtering**. That stops a device looking up a name. It
cannot stop a device that already knows an address — which matters when the server is on
your own network.

Plex is the worked example. AdGuard's `plex` service blocks four domains — `plex.tv`,
`plexapp.com`, `plex.bz` and `plex.direct`. Blocking it **does** stop sign-in, server
discovery through plex.tv, remote streaming and metadata. It does **not** stop a client
already signed in from playing off the local server (10.7.11.21): Plex clients find LAN
servers by GDM broadcast and connect straight to the IP, without ever asking DNS.

`plex.direct` looks like it should close that gap — those hostnames encode a private
address (`10-7-11-21.<hash>.plex.direct` → `10.7.11.21`) and are how clients reach a LAN
server over TLS. But on this network they don't resolve at all: the upstream resolver
(pfsense, 10.7.11.1) strips private addresses out of public answers as rebinding
protection. Verified — 1.1.1.1 returns `10.7.11.21` for that name, AdGuard returns
nothing. So blocking `plex.direct` changes nothing locally; clients already fall back to
the raw IP.

**To actually stop local Plex you have to stop Plex.** Warden ships that lever — see below.
The alternative enforcement point is pfsense: a firewall rule from the kids' devices to
`10.7.11.21` on `tcp/32400`, plus `udp/32410-32414` to stop discovery. Warden doesn't drive
pfsense today.

The same reasoning applies to anything else self-hosted. DNS filtering is the right tool
for the open internet and the wrong tool for your own LAN.

### Stopping the service itself — the NAS lever

Above the switches on the Dashboard — just under the quick-action buttons — there is a
**Whole-house services** bar. It reaches the NAS over SSH and starts or stops the real Plex process — the one thing that does close the
gap above.

It is deliberately kept apart from the per-group toggles, because **it applies to everyone**:
a stopped Plex is stopped for the whole house and anything mid-stream ends. So it asks for
confirmation, and it takes a ✋ manual hold like any other switch.

Rules can name it too — *"At 7am every day, start the Plex server"* compiles to a host-service
action. Note that only an LLM backend can produce one; the built-in parser can't.

It needs `QNAP_SSH_USER` / `QNAP_SSH_PASS` in `.env`. Without them the card shows `?` for
state rather than failing. If the start/stop commands are wrong for your NAS — Container
Station rather than a QPKG, say — there's no UI for it, so ask over the API:

```powershell
curl.exe -s http://localhost:8080/api/hosts/qnap/discover
```

That reports what's actually installed and how to address it, and you edit
`managed_services:` in `config/warden.yaml` to match.

### Xbox — cloud gaming, no console

The **Xbox** switch uses AdGuard's `xboxlive` service, which covers `xboxlive.com`,
`xboxservices.com`, `gamepass.com` and friends. Xbox Cloud Gaming streams from
`xgpuweb.gssv-play-prod.xboxlive.com` / `xhome.gssv-play-prod.xboxlive.com` — subdomains
of `xboxlive.com`, and the rule is `||xboxlive.com^`, which matches subdomains. So
blocking it does stop play.

**One gap:** `www.xbox.com` is *not* in AdGuard's list, so the launcher page still loads
— it simply can't start a game. If you want the page gone too, add a custom rule under
AdGuard → Filters → Custom filtering rules, scoped to the kids' tag:

```text
||xbox.com^$ctag=user_child
```

That is static, not tied to the Warden switch — it stays on until you remove it.

Unlike Plex there's no local-server problem here: cloud gaming is entirely remote, so DNS
filtering is exactly the right tool and nothing bypasses it by IP.

---

## 8. Settings reference (`.env`)

| Key | Notes |
| --- | --- |
| `ADGUARD_URL` / `_USER` / `_PASS` | The AdGuard admin API. |
| `ADGUARD_MODE` | `auto` \| `live` \| `fake` — see §7. |
| `WARDEN_LLM` | `api` (uses `ANTHROPIC_API_KEY`, billed) \| `cli` (reuses your Claude Code login, free) \| `auto` \| `off`. |
| `ANTHROPIC_API_KEY` | Needed for `api`. |
| `WARDEN_LLM_MODEL` | Defaults to `claude-haiku-4-5-20251001` — cheap and fast. |
| `WARDEN_EVAL_INTERVAL_MIN` | Dynamic-rule tick, in minutes. |
| `WARDEN_EVAL_MODEL` | Model for dynamic-rule evaluation only (currently `claude-sonnet-5`); blank = use `WARDEN_LLM_MODEL`. |
| `WARDEN_COLLECT_WAIT_SEC` | How long an evaluation waits for a running or overdue source collection before deciding without it. Default 180 — **per source**. |
| `WARDEN_PASSWORD` | Set it to put a login in front of everything. Empty = open access. |
| `WARDEN_USERNAME` | The name you type at that login. Defaults to `admin`. |
| `WARDEN_SECRET` | Pins the cookie-signing key. Unset, it's derived from the credentials — so changing the password signs every device out. |
| `WEDUC_USERNAME` / `WEDUC_PASSWORD` | Weduc sign-in. Atom and ParentPay need no credentials here (§4). |
| `QNAP_SSH_USER` / `QNAP_SSH_PASS` | SSH login for the whole-house services in §7. Unset = the card shows `?`. |
| `WARDEN_CLAUDE_CLI` | Name of the `claude` binary for `WARDEN_LLM=cli`. Default `claude`. |
| `WARDEN_HOST` / `WARDEN_PORT` / `WARDEN_DB` | Bind address, port and SQLite path. The container forces the DB to `/app/data/warden.db`. |
| `WARDEN_TZ` | Schedules and "today" are interpreted in this zone. |

`WARDEN_LLM=auto` prefers the API key whenever one is present — so if you want the free
CLI path locally, set `cli` explicitly rather than relying on `auto`.

With `WARDEN_LLM=off` (or no backend available) **dynamic rules are skipped entirely** —
they don't fail loudly, they just never fire. Deterministic rules carry on.

---

## 9. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| About says `fake`, expected `live` | Wrong `ADGUARD_PASS` (§7). |
| Sources shows "no saved Atom session" | Run the Atom login (§4). |
| Atom says "session expired — re-run atom login" | Google session lapsed; re-run the login. |
| A Weduc run fails: *"no live session; refusing to pass fixtures off as real"* | The session expired and couldn't be re-captured. Check `WEDUC_USERNAME`/`WEDUC_PASSWORD`, or run the login helper by hand if the portal is asking for a 2FA code. The last good data stays in place; it never substitutes fixtures for a failed live run. |
| Every dynamic rule stopped firing at once | A source's data isn't live, so Warden is refusing to act on any of it (§3). Fix the source; the red banner on Sources names it. |
| The Whole-house services card shows `?` | `QNAP_SSH_USER`/`QNAP_SSH_PASS` unset, or the NAS is unreachable (§7). |
| A dynamic rule never fires | Is a backend configured? Dry-run it and read the reason — it usually tells you exactly which condition it couldn't confirm. |
| A dynamic rule fires but does nothing | Idempotency: the switch is already where the rule wants it. Check Activity. |
| Rule saved with the wrong meaning | Read the compiled preview and warnings; rephrase, or write it as two rules (one to open, one to close). |
| Something changed and you don't know why | **Activity** tab — every action is logged with its cause and reasoning. |

Server logs: `data\warden.log` if you started it with `run-warden.cmd` or a Scheduled Task;
`data\server.log` and `data\server.err` if you used the `Start-Process` form in §1;
`docker compose logs -f warden` in a container.

---

## 10. Turning ALL protection off (the reason gate)

The Network-protection toggle on the Dashboard is the nuclear switch: off means AdGuard
filters **nothing**, for **everyone** — ads, tracking, safe search and every kids' block,
all at once. It stays available, because filtering occasionally breaks something real
("my work VPN isn't connecting"), but it is deliberately not casual:

- **Turning it off asks for a reason and a name — both required.** Both are recorded in
  the activity log; the reason is refused if it's missing, too short (under 8
  characters), or reads like a streaming unblock ("the kids want Netflix", "twitch isn't
  loading") — per-service switches exist for exactly that, the refusal names the word
  that tripped it, and **refused attempts are logged too**, so probing the gate is
  visible. The name is remembered per browser, so each device only asks once.
- **The log entry is loud.** `DISABLED by Claire's iPhone — reason: "…"` gets a red
  stripe in the Activity feed, and the lockout screen every housemate sees while
  protection is off names who turned it off, why, and when.
- **Warden stays locked until protection is back on.** Re-enabling needs no reason.

This is accountability, not access control — anyone in the house can still type a
reason and switch it off. If it keeps happening, set `WARDEN_PASSWORD` (§8) and the
whole dashboard needs a login first.

---

## 11. Two safety notes

**`scripts/smoke.py` mutates whatever it points at.** Its last step sets
`kids/youtube = allowed` and never restores it. It runs the Atom source against fixtures
(`live=false`), but a fixture run still pushes signals onto the live bus, which can make
dynamic rules fire. Run it only against a
throwaway fake-mode instance. In the terminal that runs the server:

```powershell
$env:ADGUARD_MODE="fake"; $env:WARDEN_PORT="8099"; $env:WARDEN_DB="data/smoke.db"
.venv\Scripts\python.exe -m warden
```

Then in another:

```powershell
.venv\Scripts\python.exe scripts\smoke.py http://127.0.0.1:8099
```

`scripts/test_wake_semantics.py` is the safe one — it runs offline, touches nothing, and
guards the rule wake-up rules described in §3.

**These are real controls on real devices.** A rule that blocks at 8pm but has no
counterpart to unblock will stay blocked. Dry-run first, and check Activity after.

---

## 12. Scope

Everything here controls **your own network and your own kids' devices**. The scrapers
read **your own accounts**, read-only, at a polite volume — they never submit, book or pay
anything. Credentials live only in `.env`, which is git-ignored. Rotate `ADGUARD_PASS` if it
has ever been pasted somewhere it shouldn't be.

**What it can't reach.** DNS filtering stops a lookup, not a device that already knows where
to go — hence the NAS lever in §7 for Plex. The same gap is wide open on the Amazon Fire
tablets: a film already downloaded, a Kids+ book already on the device, an installed game, or
a tablet on a mobile hotspot are all outside everything described here. There is a recon tool for it
(`python -m warden.adapters.amazon.login` to sign in once, `--watch` to record what the
dashboard calls, `--check` to re-test a saved session) and **no adapter** — no Amazon tab, no
signals, nothing wired up. Don't go looking for one.
