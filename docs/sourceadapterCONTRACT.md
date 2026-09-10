> **Historical — superseded by the shipped adapter contract.**
> This is the original platform spec, written before any adapter existed. Its §3 Item schema
> survives more or less intact, but the notification-digest product around it was never built,
> and it predates two things every real adapter returns: **Signals** and the free-form
> **`state`** dict the rules engine actually reasons over. Following this document today would
> produce an adapter the registry cannot load.
>
> **For the live contract, read [`warden/INTERFACES.md`](../warden/INTERFACES.md)**
> and [`warden/warden/adapters/base.py`](../warden/warden/adapters/base.py).

# Multi-Source Digest Platform — Core Architecture & Adapter Contract (v1)

**A central service ("the hub") that ingests from many authenticated websites via interchangeable *source adapters*, normalises everything to one item model, applies LLM relevance + summarisation per subject, and dispatches notifications or actions. Weduc is the first adapter; adding a website means writing a new adapter to this contract — the core never changes.**

This is the reusable spec. The Weduc-specific brief (`weduc-adapter-HANDOVER.md`) is one concrete implementation of it.

---

## 1. Vision & separation of concerns

```
 websites            ADAPTERS (per-source)            HUB (shared core)
 ────────            ────────────────────             ─────────────────
 Weduc      ─┐       authenticate()                   registry + scheduler
 School B   ─┼──►    collect() → Item[]      ──JSON──► dedupe / state store
 Council    ─┤       healthcheck()                     reasoning (LLM + profiles)
 GP portal  ─┘       (dumb about relevance)            action / notify dispatch
```

**The golden rule:** adapters *fetch and normalise*; they do **not** decide what matters or what to send. All relevance, summarisation, routing and notification live in the hub, once, so every source benefits from the same intelligence and the same delivery channels. A new source is pure gain with zero duplication.

### Design principles
- **Uniform contract** — adapters are hot-swappable; the hub knows nothing source-specific.
- **Isolation** — each adapter runs sandboxed with its own dependencies and narrowly-scoped credentials (a scraper for one site can't touch another's secrets). See Decision A.
- **Idempotency** — every item has a stable `external_id`; the hub dedupes centrally so nothing is actioned twice.
- **Stateless adapters** — the hub owns cursors/state and passes `since` to the adapter; adapters keep no long-term state of their own where avoidable.
- **Contract-first** — JSON Schemas for the item model and adapter I/O are the source of truth; adapters validate their output against them in CI.

---

## 2. The Adapter Contract (v1)

Any source adapter — present or future — implements exactly this. Nothing more is required of it; nothing less is accepted.

### 2.1 Manifest (static metadata the adapter advertises)
```json
{
  "id": "weduc",
  "display_name": "Weduc / ReachMoreParents",
  "version": "1.0.0",
  "auth_type": "form_login_session",
  "capabilities": ["messages", "calendar", "forms", "meals", "newsletter", "posts"],
  "schedule_hint": "3x daily",
  "secrets_required": ["username", "password"]
}
```

### 2.2 Operations
- **`authenticate(secrets) → session_state`**
  Establish a session; return a persistable, refreshable blob (cookies / storage state / token). Must handle re-auth transparently when expired.
- **`collect(session_state, since_cursor) → { items: Item[], next_cursor, health }`**
  Fetch everything new since `since_cursor`, normalised to the Item schema. Return an opaque `next_cursor` for the hub to store. **No filtering for relevance** — return all items the account can see; the hub decides.
- **`healthcheck() → { ok, detail }`**
  Cheap liveness/structure check so the hub can alert if a site changes shape or auth breaks.

### 2.3 Reserved for v2 (design for it, don't build it yet)
- **`act(session_state, action) → result`** — two-way actions (submit a form, mark read, book a meal, RSVP). Keep the contract shape ready; leave unimplemented in v1.

### 2.4 Integration style — **Decision A (recommended: process + JSON)**
Each adapter is a self-contained **executable exposing a JSON-over-stdio (or small HTTP) protocol**, e.g.:
```
weduc-adapter collect --since <cursor> --secrets-ref <vault-key>   # → Item[] JSON on stdout
```
*Why:* language-agnostic (a stubborn site can use whatever scraping stack fits), strong isolation (own container/venv, scoped creds), independently testable and deployable, and the hub only ever speaks JSON.
*Alternative:* in-process library plugins — simpler and lighter, but single-language and weaker isolation. Pick per appetite; the contract is identical either way.

---

## 3. Normalised Item schema (the lingua franca)

Every adapter emits items in exactly this shape. This is what makes sources interchangeable.

```json
{
  "source_id":    "weduc",
  "account_id":   "cliff",              // which configured account/login
  "subject_ids":  ["luke"],            // who this concerns, if the adapter can tell; else []
  "external_id":  "weduc:msg:88213",   // STABLE, unique — drives dedupe
  "kind":         "message",           // message|calendar_event|form|newsletter|payment|post|other
  "title":        "Jellyfish – games for Friday",
  "body_text":    "Jellyfish Class can bring in a game ...",
  "occurred_at":  "2026-07-16T11:06:00Z",
  "due_at":       null,                 // for forms/actions/deadlines
  "audience_tags":["Jellyfish Class"], // raw source tags; hub maps to subjects
  "url":          "https://ui.app.weduc.co.uk/message/...",  // deep link back
  "attachments":  [{"kind":"pdf","url":"...","mime":"application/pdf"}],
  "raw":          { "...": "original payload, kept for the LLM" },
  "fetched_at":   "2026-07-21T13:49:00Z"
}
```

Notes:
- `external_id` must be stable across runs — dedupe depends on it. If the source gives no id, hash stable fields (kind+date+title+body).
- `audience_tags` are raw (e.g. class names, "whole-school"); the **hub** maps these to subjects via profiles — the adapter needn't know who "Luke" is.
- `raw` and `attachments` are preserved so the reasoning layer can read originals (e.g. a newsletter PDF).

Contracts live as JSON Schema in `/contracts` and are versioned; adapter output is schema-validated in CI.

---

## 4. Hub: the shared core

1. **Adapter registry** — discovers adapters via their manifest; enables/schedules per config.
2. **Scheduler / orchestrator** — runs each adapter on its cadence, passes the stored cursor, collects Item[].
3. **Secret store** — vault (OS keyring / SOPS / cloud secret mgr); adapters get scoped refs, never raw secrets in config.
4. **State + dedupe store** — SQLite/Postgres; per-adapter cursors + seen `external_id`s.
5. **Reasoning service (LLM, central)** — see §5.
6. **Action / notification dispatch** — pluggable channels + routing rules.
7. **Audit / logging / health** — every run, every decision, every send recorded; health alerts on adapter breakage.

---

## 5. Reasoning layer (central, shared — **Decision B: keep it in the hub**)

Filtering/summarisation is generic and lives once in the hub so all sources are treated consistently.

**Subject profiles** drive it:
```json
{
  "subject_id": "luke",
  "name": "Luke Dabbs",
  "contexts": [
    { "label": "Jellyfish Class", "effective_to":   "2026-09-02" },
    { "label": "Swordfish Class", "effective_from": "2026-09-02" }
  ],
  "interests": ["forms/deadlines", "trips", "meals actions", "whole-school essentials"],
  "drop": ["other classes", "community adverts", "third-party promos"]
}
```

The reasoning step takes **new items + the relevant subject profile(s)** and returns a relevance verdict plus a digest fragment (or "nothing new"). Because it's date-aware, the Jellyfish→Swordfish rollover is automatic. It sees `raw`/attachments so it can read source PDFs. It's identical logic whether the item came from Weduc or the next website — that's the whole point.

---

## 6. Action / notification layer

Channels are plugins behind one interface (`send(target, message)`): Telegram, Pushover, email/SMTP, WhatsApp-via-Twilio, etc. Routing rules map (subject, urgency) → channel(s). Only new+relevant results dispatch. Same open channel decision as before — Telegram/Pushover easiest and most robust for unattended; WhatsApp is the awkward one; native WhatsApp desktop app is not automatable.

---

## 7. Suggested repo layout (monorepo)

```
digest-platform/
  contracts/            # JSON Schemas: item, manifest, collect-output  (source of truth)
  core/
    registry/ scheduler/ state/ secrets/ reasoning/ dispatch/ config/
  adapters/
    weduc/              # adapter #1  (see weduc-adapter-HANDOVER.md)
    <next-site>/        # adapter #2  — proves reusability
  channels/
    telegram/ pushover/ email/ whatsapp_twilio/
  tests/
    fixtures/           # saved captures per adapter for offline tests
  README.md
```

---

## 8. Build order

1. **Freeze contract v1** — write the JSON Schemas in `/contracts`. Everything keys off these.
2. **Core skeleton** — registry, scheduler, state/dedupe, one channel, reasoning stub.
3. **Weduc adapter** to the contract (the recon is done — see its brief).
4. **Reasoning** — profiles + prompt; wire to real items.
5. **Second adapter** — even a trivial one (an RSS feed / another portal) to *prove* a new source needs zero core changes. This is the acceptance test for "reusable".
6. **Harden** — health checks, re-auth, retries, audit, alerting on adapter breakage.

---

## 9. Open decisions (platform-level)

- **A — Integration style:** process+JSON (recommended, isolatable, polyglot) vs in-process plugins (simpler, single-language).
- **B — Reasoning location:** central in hub (recommended, consistent) vs per-adapter (flexible but duplicated).
- **C — Language/stack:** Python recommended for core + adapters (Playwright, `anthropic` SDK), but process+JSON lets individual adapters differ.
- **D — Secret storage:** OS keyring / SOPS / cloud secret manager.
- **E — Hub deployment:** where it runs (the always-on VM), and how adapters are packaged (containers vs venvs).
- **F — State store:** SQLite (simple, single-box) vs Postgres (multi-source scale).

---

*This contract is the reusable heart of the project. Read it first; then read each adapter brief as an implementation of §2–§3. The measure of success is that adapter #2 slots in without touching `core/`.*
