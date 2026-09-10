> **Historical — recon as of 21 July 2026, superseded by
> [`warden/warden/adapters/atom/`](../warden/warden/adapters/atom/).**
> An honest record of what the portal looked like when it was mapped; the code has moved past
> it. Most importantly, **auth is Google SSO**, not the form login assumed here — the session
> is captured once by hand (`python -m warden.adapters.atom.login`) and then replayed
> headlessly. The shipped adapter also emits four signals and derives the weekly island target
> from Atom's own published plan, neither of which appears below.

# Adapter Brief — Atom Learning (Source Adapter #2)

**A source adapter for the multi-source digest platform. It reads a child's Atom Learning account and returns normalised `Item[]` — principally: what work has been set, and what work Luke has actually done (with timestamps). It does NOT decide relevance or notify — that's the hub.**

> Read `source-adapter-CONTRACT.md` first; this implements §2–§3 of it. This is the second adapter, and its main purpose is to **prove the platform is reusable** — it slots into the same contract as Weduc with zero core changes.

Reconnaissance performed live on 21 July 2026 against the logged-in account. **Unlike Weduc, Atom exposes a clean JSON API** — this adapter should hit it directly and skip DOM scraping almost entirely.

---

## 1. Headline finding — this one is easy

Atom is a single-page app backed by a well-structured REST API at **`api.atomlearning.com`**. Auth is a **HttpOnly session cookie scoped to `.atomlearning.com`**, so once logged in, API calls from a cookie-bearing session authenticate automatically (verified: `GET .../subjects` → `200` with JSON). No token juggling, no page scraping needed for the data that matters.

**The "has Luke done his work?" question is answered directly by two endpoints:**
- **`learning-resources`** — the set-work / to-do list (what was assigned, by whom, due when, started or not).
- **`islands`** — the full completion history per topic, each with a `status` and a `dateCompleted` timestamp.

Live proof from the recon: 410 island records, **394 completed / 16 not_completed**, and the most recent completions were **today, 21 Jul 2026** ("Homographs" 16:06, "Our Solar System and the Universe" 15:58) — so recency of effort is precisely knowable.

---

## 2. Access & identity (verified)

- **App URLs:** `https://app.atomlearning.com/accounts/` (profile picker — "Atom Home"), `https://app.atomlearning.com/student/` (the child dashboard, tabs **Learn / Track / Test**).
- **Auth:** HttpOnly cookie on `.atomlearning.com`. No JS-visible token (localStorage only holds `unleash:*` feature-flag + `theme-*`; no bearer in storage). The adapter needs a logged-in session cookie jar; login itself is a form auth (username/password; no 2FA reported on this account — confirm).
- **API base:** `https://api.atomlearning.com/` with microservice prefixes: `ms_learning`, `ms_accounts`, `ms_courses`, `ms_subscriptions`, `ms_sockets` (socket.io, realtime — ignore for polling).
- **Account → child:** `GET /ms_accounts/user` returns the account; the accounts page lists child profiles (here just **Luke**). Each child has a **student id** used in every learning call.
- **Luke's student id:** `_18788523414932504` (note the leading underscore; treat as opaque string). Discover it per-account rather than hardcoding.

---

## 3. Key endpoints (all GET, cookie-authenticated, under `/{ms}/students/{studentId}/`)

**Primary (the work-done signal):**

| Endpoint | What it gives | Use |
|---|---|---|
| `ms_learning/.../learning-resources` | Set-work / to-do list | what was assigned + started/due state |
| `ms_learning/.../islands` | Per-topic completion history (410 items) | what's actually been done, with timestamps |
| `ms_learning/.../subjects` | Subject config for the child | subject list, target grades |

**Supporting / context (captured, not all inspected):**
`ms_learning/.../learning-journey-targets`, `ms_learning/.../learning-journeys/stories`, `ms_learning/.../treasure-summaries`, `ms_learning/.../shop-items`, `ms_subscriptions/.../enrolments`, `ms_accounts/students/.../school_links`, `ms_courses/.../quotes/suggested-quote`, `ms_accounts/user`.

### 3.1 `learning-resources` — the to-do / set work
Array of items. Confirmed fields:
```
name              e.g. "(2) The Kent Test: English & Maths", "English"
type              "MOCK_TEST" | "PRACTICE"
id_question_session  stable id of the work session
id_course_subject    subject ref (nullable for cross-subject mocks)
started           ISO timestamp OR null   ← null = NOT STARTED
dueDate           ISO timestamp OR null   ← deadline, if set
setBy             { fullId, name, roles:{sup,prnt} }  ← who assigned it
createdAt         ISO timestamp
customName, canDelete, id_exam_config, hash
```
Observed rows included work `setBy` "CliffDabbs" (parent) and by "Luke" himself — the `roles` flag distinguishes parent-set vs self-set. Several had `started: null` (assigned but not begun).
> Note: `learning-resources` shows *started/not-started + due*. Whether a started MOCK_TEST is **finished** is best confirmed via the linked `id_question_session` (see 3.3) or the islands data; don't assume started == complete.

### 3.2 `islands` — the completion history (the strongest signal)
Array (~410 items for Luke). Confirmed fields:
```
title             e.g. "Homographs", "Our Solar System and the Universe"
type              "practice" (island types)
status            "completed" | "not_completed"
dateCompleted     ISO timestamp (present when completed) ← recency of work
id_island_progression, id_course_atom, id_course_subject
sessions[]        { id_question_session, sessionHash, completed, ... }  ← per-attempt detail incl. scores
```
This yields, per subject: islands completed vs total (matches the dashboard's "1/6 islands done"), **and** the exact date/time each was completed — so "did he do anything today / this week?" and "which subjects are being neglected?" are trivial to compute.

### 3.3 Per-session detail
Each `sessions[]` entry (and each `learning-resources.id_question_session`) references a question session; there is almost certainly a `ms_learning` session/results endpoint returning score/accuracy/answers for a given `id_question_session`. **Not yet mapped** — see §6. Needed only if the digest should report *scores*, not just completion.

### 3.4 Subject code mapping
Subject refs seen: `235`, `236` (verbal reasoning — "Letter Logic/Hidden Words/Letter Strings"), `239` (science — "Solar System"); dashboard subjects were **English, VR, NVR, Science**. `subjects` returns `id_course_subject` but with `abbreviation:null`/`color:null` here, so **human names likely come from a `ms_courses` catalogue endpoint** — map ids→names there (see §6).

---

## 4. Mapping to the platform `Item` schema

This adapter emits items the hub can reason over. Suggested mapping:

- **Set work → `kind: "assignment"`** (from `learning-resources`):
  `external_id = "atom:resource:" + id_question_session`; `title = name`; `due_at = dueDate`; `occurred_at = createdAt`; `subject_ids = ["luke"]`; `audience_tags = [subjectName, type]`; `raw` = the record. Include derived `status` (not_started / started) in `raw`.
- **Completion → `kind: "activity"`** (from `islands`, `status=completed`):
  `external_id = "atom:island:" + id_island_progression`; `title = title`; `occurred_at = dateCompleted`; `audience_tags = [subjectName, "practice"]`; `raw` = record (incl. sessions).
- **Optional roll-up → `kind: "progress_summary"`**: per-subject completed/total + last-activity date, so the hub can say "no Atom work done in N days" without reprocessing 410 rows. (Either the adapter computes this, or the hub does — keep the adapter faithful and let the hub decide; a summary item is a convenience.)

The hub's subject profile + reasoning then turn these into the parent-facing line ("Luke did 2 practice topics today; the Kent Test mock set on 4 Feb still not started"). **The adapter must not make that judgement** — it just supplies started/`dueDate`/`dateCompleted` faithfully.

---

## 5. `collect()` shape for this adapter

```
authenticate(secrets)  → login, hold .atomlearning.com cookie jar
collect(session, since):
    1. GET /ms_accounts/user + accounts    → resolve child profile(s) + studentId(s)
    2. per student: GET learning-resources, islands, subjects  (+ ms_courses names)
    3. normalise → Item[]  (assignment + activity [+ progress_summary])
       - filter islands to dateCompleted >= since for incremental runs
    4. return { items, next_cursor: maxDateCompleted, health }
healthcheck()          → GET subjects returns 200 + expected keys
```
Incremental strategy: the hub passes `since`; the adapter returns only islands completed since then (plus current open assignments). `next_cursor` = latest `dateCompleted` seen.

---

## 6. Still to map (small, optional)

1. **Session results endpoint** — for scores/accuracy per `id_question_session` (only if the digest should report marks, not just done/not-done). Capture by opening a completed activity's results in the UI with the network tab on.
2. **Subject id→name catalogue** — the `ms_courses` endpoint that names `id_course_subject` (235/236/239 → English/VR/NVR/Science).
3. **Track tab** — not visually explored (renderer stalled during recon); it likely surfaces accuracy-over-time/analytics. The underlying data is already largely in `islands`, so this is a nice-to-have, not a blocker.
4. **Login flow specifics** — confirm the exact login form/SSO (Google SSO button was present on the account page) and whether the account uses email/password or Google sign-in; shapes the `authenticate()` implementation.

---

## 7. Gotchas

- **API-first, not DOM.** The clean JSON API is the whole story here — prefer it entirely; only touch the DOM if a needed datum has no endpoint. This makes the adapter far more robust than the Weduc one.
- **Cookie is HttpOnly** — the adapter must persist the *cookie jar* from login (Playwright storage state or an `httpx` cookie jar), not a token it can read from JS.
- **`started != completed`** for mock tests — confirm completion via session/islands, not just the `started` flag.
- **Stable ids** — use `id_question_session` / `id_island_progression` for `external_id`; they're stable and dedupe-friendly.
- **Volume** — `islands` is ~189 KB / 410 rows; fetch once and filter by `dateCompleted >= since`. Cheap.
- **Realtime socket** (`ms_sockets/socket.io`) is for the live app; ignore it — polling the REST endpoints on the hub's schedule is correct for a digest.
- **Read-only** — never POST/mutate (no setting or deleting work). `act()` stays deferred per contract.
- **Self-set vs parent-set** — `setBy.roles` distinguishes; the hub may want to treat "work Dad set that isn't done" differently from Luke's own practice. Surface `setBy` faithfully so it can.

---

## 8. Why this validates the platform

Two very different sites — Weduc (messy SPA, scrape + PDF) and Atom (clean JSON API) — both reduce to the **same three operations and the same `Item` schema**. The hub's reasoning and notification layers don't change at all to add Atom; only a new adapter appears under `adapters/atom/`. That's the reusability claim, demonstrated.

Example of the combined value the hub could then deliver, drawing on both adapters:
> *"Luke did 2 Atom practice topics today (Homographs, Solar System). ⚠️ The Kent Test mock set on 4 Feb is still not started. School: 2 Weduc forms outstanding; term restarts Wed 2 Sep."*

---

*End of adapter #2 brief. Recommended first action for Claude Code: confirm the login flow (§6.4), then wire `learning-resources` + `islands` → Item[] per §4.*
