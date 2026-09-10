> **Historical — recon as of 21 July 2026, superseded by
> [`warden/warden/adapters/weduc/`](../warden/warden/adapters/weduc/).**
> An honest record of what the portal looked like when it was mapped. Since then: the two-host
> split (API on `app.weduc.co.uk`, SPA on `ui.app.weduc.co.uk`) turned out to be the thing that
> matters most, meals and the school calendar became first-class collections, and the newsletter
> is read by the hub rather than forwarded as a PDF the way §4 imagines.
>
> *(Previously filed as `weduclukedigestHANDOVER (2).md`.)*

# Adapter Brief — Weduc (Source Adapter #1)

**A source adapter for the multi-source digest platform. It authenticates to the Weduc / ReachMoreParents parent portal, collects everything the account can see, and returns it as normalised `Item[]` to the hub. It does NOT decide relevance or send notifications — that is the hub's job.**

> Read `source-adapter-CONTRACT.md` first. This document is one concrete implementation of §2–§3 of that contract. Platform-level concerns (scheduling, dedupe, reasoning/LLM, notification channels, subject profiles) live in the hub and are **out of scope for this adapter**.

Everything below the "Reconnaissance" heading was observed directly by logging into the live portal on 21 July 2026; treat selectors and IDs as *starting points to verify*, not guarantees.

---

## 1. Scope of this adapter

Implement the three contract operations for Weduc:
- **`authenticate(secrets)`** — form login (username/password), persist + refresh session.
- **`collect(session, since)`** — pull new items across all sources below, normalise to the platform Item schema, return with a `next_cursor`.
- **`healthcheck()`** — confirm login + page structure still valid.

The end-user goal it ultimately serves (context only, delivered by the hub, not here): a short phone digest of everything relevant to the child **Luke Dabbs**, a few times a day, silent when nothing's new. The relevance filtering, the Jellyfish→Swordfish rollover logic, and the delivery channel are all **hub responsibilities** — this adapter just returns faithful, complete, normalised data with good `audience_tags` so the hub can do its job.

### What "relevant" means
- Keep: anything about Luke personally, his class, upcoming dates affecting him, forms/actions with deadlines, meal-payment actions, and whole-school essentials (term dates, closures, immunisations, reports).
- Drop: other classes' chatter, generic community adverts (local festivals, third-party promos), and platform/product noise.
- Note the **class rollover**: Luke is in **Jellyfish Class** until end of the 2025/26 year and moves to **Swordfish Class** from Wed 2 Sept 2026. Filtering must be *date-driven config*, not hardcoded, so it follows him automatically.

---

## 2. Adapter-specific notes (context for the developer)

- The portal **requires authentication**. There is **no 2FA** on this account, and credentials come from the hub's secret store (scoped ref), never hardcoded. This makes headless login viable.
- This adapter is **dumb about relevance** by design. It must NOT filter to Luke, summarise, or decide what to send — it returns *all* items the account can see, each tagged with `audience_tags` (class names, "whole-school") and `subject_ids` where determinable. The hub's reasoning layer + subject profiles do the filtering. (This is what keeps the platform reusable — see contract §1, §5.)
- Preserve `raw` payloads and `attachments` (especially the newsletter PDF, §4) so the hub's LLM step can read originals.

---

## 3. Reconnaissance — the Weduc portal (verified 21 Jul 2026)

**Platform:** Weduc, branded "Reach More Parents by Weduc". Single-page app (JS-rendered; plain HTML scraping of `get_page_text` returns mostly chrome/nav — content is loaded dynamically).

**Base URL:** `https://ui.app.weduc.co.uk/`
**Login:** `https://ui.app.weduc.co.uk/login` — simple form: "Login or E-mail" field, "Password" field, "Login" button. No 2FA. Session-cookie based after login.

**School:** Seabrook Church of England Primary School.
**Account holder:** Cliff Dabbs — internal user id observed as `281474978315305`.
**Child:** Luke Dabbs — internal id observed as `281474978315151`; DOB 02/04/2017; currently **Jellyfish Class**. Second parent on record: Asta Dabbs.

### Navigation / data sources
Left-hand nav, grouped:

**Communications**
- **Newsfeed** — `/dashboard/newsfeed/list/user/{userId}`. Has a per-child filter ("LUKE DABBS") that switches the feed to Luke's id. Posts carry audience tags in their header, e.g. `School Website, All Staff, Jellyfish Class` or `... Seabrook Church of England Primary School` (whole-school). **The newsletter is posted here as a PDF attachment** ("Newsletter 17th July 2026.pdf") — see §4.
- **Messages** — `/message/message/index`. Folders: Inbox (393), Sent, Deleted, Notifications (538), Pending, Tags, Folders, SMS replies. Senders seen: "J Beech" (appears to be head/office) and "Office mailbox". Mix of class-specific ("Jellyfish – games for Friday") and whole-school ("New Build Update", "Uniform Consultation").
- **Calendar** — `/calendar/event/index`. Filter **Display = "Children and My Group(s) Events"** narrows to the child. Month/week/day/list views. Real events observed: class trips, Sports Day, INSET days, "End of Term 6", "Term 1 Starts" (Wed 2 Sep 2026), "Flu Immunisation" (Wed 30 Sep 2026).
- **Notices** — had an unread badge (1); **not explored** — investigate.
- **Forms** — `/forms/index/forms/user/{childId}`. Tabs: **Available Forms** (outstanding) and **Submitted Forms**. Per-child. Two outstanding for Luke: "Interest in choir for the new school year (2026/2027)" and "Permission for School to Administer Short Term Prescription Medicine".
- **Digital library**, **The Hub**, **Home Learning** — **not explored**; may hold homework/resources. Investigate for relevance.

**Parent portal** → **Luke Dabbs** profile (`/assessment/parent/index/id/{childId}`). Accordion sections: Contact Info, Absences, Attendance, **Meals**, Groups.
- **Meals** — an "Order Meals" calendar with colour-coded status: **grey = No Action Required, orange = Incomplete Order, red = Unpaid Order, green = Completed Order**. Plus "Order History / Transactions" and "Balance Adjustments" tabs. This is the source for meal-payment actions (red/orange = needs attention).

**Payments** — nav group, **not explored**; likely dinner-money / trip payments. Investigate for outstanding-balance signals.

### Critical technical recommendation
Because the UI is a SPA, the cleanest and most robust approach is almost certainly to **capture the underlying JSON/XHR API calls** the app makes (browser devtools → Network, or Playwright request interception), rather than scraping the rendered DOM. The REST-ish URL patterns (`/dashboard/newsfeed/list/user/{id}`, `/calendar/event/...`, `/forms/index/forms/user/{id}`) strongly suggest JSON endpoints behind them. **First dev task: log in, open the network tab, and map the real data endpoints + auth mechanism (session cookie vs bearer token).** If clean JSON APIs exist, prefer them; fall back to Playwright DOM scraping only where no API is exposed.

---

## 4. The newsletter (important, and easy to miss)

The weekly newsletter is a **multi-page PDF** attached to a newsfeed post — separate from the post text. It contained **class-specific information that appeared nowhere else in the portal**, e.g. a Swordfish Class bell-boat result and a Canada Day service, plus a per-class "Merit Book" naming individual children (a likely place for Luke to be named). It also carries term-only items (Summer Reading Challenge, School Games award, per-year attendance).

**Implication:** the collector must fetch the PDF attachment and pass it to the LLM. Claude's API accepts PDFs directly (as a document content block) — no separate OCR needed for digital PDFs. The per-class and per-child mentions inside the newsletter are high-value and must be in scope.

---

## 5. Adapter internals (what this module does)

Scope is only the shaded box — everything after `collect()` returns is the hub.

```
authenticate(secrets)  → session_state (cookies/storage; refreshable)
collect(session, since):
    1. fetch     → newsfeed, messages, calendar, forms, meals, newsletter PDF
    2. normalise → Item[] per platform schema (external_id, kind, audience_tags, url, raw, attachments)
    3. return    → { items, next_cursor, health }
healthcheck()          → login OK + expected structures present
                       ─────────────────────────────────────────
                       ▲ hub takes over: dedupe · reason(LLM) · notify
```

### Principles
- **Mechanical only.** Login, fetch, parse, normalise. No relevance decisions, no summarising, no sending — the hub owns those.
- **Faithful tagging.** Populate `audience_tags` accurately (class names, "whole-school") and set `subject_ids` when the source makes the child explicit (per-child feed/forms/profile). This is what lets the hub filter well.
- **Stable ids.** Give every item a stable `external_id` (prefer the source's own id from the JSON APIs; else hash kind+date+title+body) so the hub can dedupe.
- **Preserve originals.** Keep `raw` and attach the newsletter PDF as an `attachment` for the hub's LLM step.

### Suggested stack (this adapter only)
- **Python** — Playwright for browser work; `httpx` if direct JSON APIs are found (§3 recommends hunting for them). **No `anthropic` SDK here** — reasoning is the hub's.
- Exposes the contract via CLI/JSON per **Decision A** (e.g. `weduc-adapter collect --since <cursor> --secrets-ref <ref>` → Item[] on stdout), or as an in-process plugin if the platform chose that style.
- Secrets arrive from the hub's store via a scoped ref; nothing hardcoded.

### Adapter repo slot
```
adapters/weduc/
  manifest.json          # id, capabilities, auth_type, secrets_required
  src/
    auth.py
    collectors/          # newsfeed.py, calendar.py, forms.py, meals.py, messages.py, newsletter.py
    normalise.py         # → platform Item schema (validated against /contracts)
    cli.py               # collect / healthcheck entrypoints (JSON I/O)
  tests/fixtures/        # saved HTML/JSON/PDF captures for offline tests
```

---

## 6. Build plan / milestones (this adapter)

1. **Endpoint recon** — log in manually, capture network traffic, document real data endpoints + auth. Decide API-vs-DOM per source. *(De-risks everything else.)*
2. **Manifest + auth module** — write `manifest.json`; headless login; persist session/storage state; auto-refresh on expiry; `healthcheck()`.
3. **Collectors** — one per source (newsfeed, messages, calendar, forms, meals, newsletter PDF). Each yields normalised `Item`s. Build against saved fixtures first.
4. **Normalise + schema-validate** — map to the platform Item schema; validate output against `/contracts` in CI; stable `external_id`s; faithful `audience_tags`/`subject_ids`; attach the newsletter PDF.
5. **CLI/JSON entrypoint** — `collect --since` and `healthcheck` per Decision A; `--dry-run`/pretty-print for local testing.
6. **Cursor handling** — accept `since_cursor` from the hub, return `next_cursor`; keep the adapter stateless.
7. **Hardening** — session-expiry recovery, polite rate limiting, health check that flags structural change, clear failure output.

*Out of scope here (hub's job): dedupe/state, the LLM reasoning + subject profiles, notification channels, scheduling. See the contract.*

---

## 7. Open items — adapter-level only

*(Platform-level decisions — delivery channel, Anthropic API key, reasoning location, run frequency, hosting — live in `source-adapter-CONTRACT.md` §9, not here.)*

1. **Sources still to map** — Notices, Home Learning, The Hub, Payments were noted but not opened (§3). Confirm which are in scope and recon them.
2. **API vs DOM per source** — output of the endpoint-recon step; determines collector implementation.
3. **`subject_id` derivability** — confirm the adapter can reliably tell *which child* an item concerns (per-child feed/forms/profile make this easy; whole-school posts get `subject_ids: []` and rely on `audience_tags`).
4. **Email-source shortcut** — does Weduc email notifications to the user's Hotmail? If so, a future *email adapter* (a separate source adapter, IMAP/Outlook) could supply messages/newsletters more robustly than scraping — a clean example of the platform's reusability rather than a change to this adapter.

---

## 8. Risks & gotchas

- **SPA fragility** — prefer discovered JSON APIs over DOM scraping; DOM will break on UI updates. `healthcheck()` should catch structural change and let the hub alert.
- **Class rollover (Jellyfish → Swordfish, 2 Sep 2026)** — the adapter just tags items with `audience_tags`; the *hub's* date-driven subject profile handles the switch. The adapter must not hardcode a class.
- **Session expiry / bot detection** — persist and refresh sessions; throttle politely; realistic user-agent; don't hammer.
- **Terms of Service** — automated access is a personal, read-only use of the user's own account. Low practical risk, but note it's likely outside Weduc's intended use; keep volume minimal and never modify/submit anything (v1 is read-only; `act()` is deliberately deferred).
- **PDF variability** — newsletters are image-rich; the adapter attaches the PDF; the hub's LLM step reads it natively (no brittle text extraction).
- **Stable `external_id`** — dedupe (in the hub) depends on it; if the source gives no id, hash stable fields and test collisions/repeats hard.
- **Secrets hygiene** — credentials come from the hub's secret store via scoped ref; nothing hardcoded, nothing committed.

---

## 9. What was already proven (so the dev isn't starting cold)

A full manual pass was completed live: logged into the portal, read the newsfeed, calendar, forms, meals, messages **and** the 6-page newsletter PDF, and confirmed every source is reachable and parseable — including catching a Swordfish item buried on page 3 of the PDF that existed in no other source. The relevance filtering was also validated end-to-end (Luke/Jellyfish/Swordfish, noise dropped). **That filtering is the hub's job, not this adapter's** — but it's proven achievable, so the platform's reasoning layer has a known-good target. The digest the hub should ultimately be able to produce from this adapter's items:

```
📚 Luke — Seabrook School update
✅ Needs doing: 2 forms (choir interest; medicine permission)
📅 Key dates: Tue 1 Sep INSET (closed) · Wed 2 Sep Term 1 starts · Wed 30 Sep Flu immunisation
🐟 Class (Jellyfish now → Swordfish Sep): end-of-term roundup; Swordfish came 2nd in bell-boat comp
🏫 Whole-school: reports sent home; clubs finished; new-build update
🍽 Meals: nothing outstanding
```

This adapter's only responsibility is to hand the hub faithful, complete, well-tagged `Item[]` so that digest is possible.

---

*End of adapter brief. First recommended action for Claude Code: `source-adapter-CONTRACT.md` §8 step 1 (freeze the contract), then §6 step 1 here (Weduc endpoint recon).*
