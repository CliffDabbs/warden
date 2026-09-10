> **Historical — this is a build brief, not a reference.**
> It was written *before* Warden's modules existed, to hand each one to a separate agent to
> implement in parallel; that is why it is written in the imperative and insists nothing be
> changed. Every module in it now ships, and parts of it were never built as specified —
> notably the Atom adapter, which is an httpx JSON-API client with a captured Google session,
> not the Playwright scraper described here.
>
> **For the live contract, read [`warden/INTERFACES.md`](../warden/INTERFACES.md).**
> Kept because it records the reasoning behind several seams that are still load-bearing.

# Warden — module interface contract (FROZEN)

You are implementing ONE module of Warden. The **foundation is already written** and
must not be changed. Your module plugs into it through the exact names/signatures below.
Read the foundation files you depend on before writing:

- `warden/models.py` — domain + view models (Item, Signal, ServiceState, StateSnapshot, GroupState, ClientState, AdGuardStatus, AuditEntry, SourceRun, WardenConfig + config models)
- `warden/rules/schema.py` — the compiled rule AST (Trigger/Condition/Action unions, CompiledRule, Rule)
- `warden/rules/vocab.py` — Vocabulary (compiler input)
- `warden/adapters/base.py` — SourceAdapter, SourceContext, CollectResult, HealthResult, AdapterManifest
- `warden/signals/bus.py` — SignalBus (emit/get/all/subscribe/history)
- `warden/db.py` — DB (all persistence)
- `warden/context.py` — AppContext + EventHub (`ctx.audit(...)`, `ctx.after_change(...)`, `ctx.events`)
- `warden/config.py` — Settings, load_config, resolve_secrets
- `warden/main.py` — how everything is wired (READ THIS to see exactly how your class is constructed and called)

## Ground rules
- **Python 3.14, async-first.** Type-hint everything. Match the foundation's style (concise, commented at the seams).
- **No new heavy dependencies.** Only what's in `requirements.txt` (fastapi, uvicorn, pydantic, httpx, apscheduler, croniter, anthropic, pyyaml, python-dotenv, jinja2, playwright).
- **Only create the files listed for your module.** Do not edit foundation files or other modules' files.
- The venv is `warden/.venv/Scripts/python.exe`. You may run `.venv/Scripts/python.exe -c "import warden.<your_module>"` from the `warden/` dir to check your module imports. **Do not** start the server and **do not** POST/PUT/DELETE to the live AdGuard at 10.7.11.29 (read-only GETs are fine if useful).
- Every mutation of AdGuard state must be auditable + push a live update. **Callers** (API routes, engine) do this by wrapping service calls with `await ctx.audit(actor, action, target, detail)` then `await ctx.after_change(reason)`. The AdGuardService itself stays pure (no audit/events).

---

## AdGuard REST facts (verified live, v0.107.78 @ http://10.7.11.29, HTTP Basic auth)
- `GET  /control/status` → `{version, protection_enabled, running, ...}`
- `GET  /control/clients` → `{clients:[<client>], auto_clients:[...]}`. A **client** object:
  ```json
  {"name":"Asta iPad","ids":["10.7.11.31"],"tags":["user_child"],
   "blocked_services":["youtube","tiktok","roblox"],
   "use_global_settings":false,"use_global_blocked_services":false,
   "filtering_enabled":true,"parental_enabled":true,"safebrowsing_enabled":true,
   "safesearch_enabled":true,
   "safe_search":{"enabled":true,"google":true,"bing":true,"duckduckgo":true,"ecosia":true,"pixabay":true,"yandex":true,"youtube":true},
   "blocked_services_schedule":{"time_zone":"Local"},
   "upstreams":[],"ignore_querylog":false,"ignore_statistics":false,
   "upstreams_cache_size":0,"upstreams_cache_enabled":false}
  ```
- `POST /control/clients/update` body `{"name":"<existing name>","data":<full client object>}` — **overwrites the whole client**, so GET-merge-PUT: fetch current, change only `blocked_services`, send the rest back intact.
- `POST /control/clients/add` body `<full client object>`
- `GET  /control/blocked_services/all` → `{blocked_services:[{id,name,...}]}` (136 services)
- `GET  /control/filtering/status` → `{enabled, user_rules:[str], filters:[...]}`
- `POST /control/filtering/set_rules` body `{"rules":[<all user rules>]}` — replaces the whole user-rules list (GET-merge-PUT to add/remove one).
- `POST /control/protection` body `{"enabled":bool}` (toggle whole-network protection).
- Real configured clients: `Asta iPad`(kids/user_child), `Living Room TV`, `Shield`, plus adult/other devices. Group membership comes from **config**, matched to AdGuard clients by `name`.

**Service state model (canonical):** a service is *blocked* for a group when its id is in
`blocked_services` of the group's clients, *allowed* otherwise.
- `set_service(group, svc, blocked)` = add `svc` to each group client's `blocked_services`.
- `set_service(group, svc, allowed)` = remove `svc`.
- `set_group(group, blocked)` = add **all managed** service ids (config.services_for_group) to each client → "disable all devices in group".
- `set_group(group, allowed)` = remove all managed service ids.
- In a snapshot, a group's service toggle is `blocked` iff every client in the group blocks it (no clients ⇒ use config default_blocked); `group.all_blocked` iff all managed services blocked.

---

## MODULE A — `warden/adguard/{client.py,service.py,fake.py}`
Implement AdGuard control, live + offline-fake, behind one class.

**`service.py`**
```python
def build_adguard(settings: Settings, config: WardenConfig) -> "AdGuardService": ...
class AdGuardService:
    mode: str            # "live" | "fake"
    url: str
    async def connect(self) -> None            # probe; under settings.adguard_mode=="auto" pick live if GET /control/status ok + creds present, else fake. Idempotent.
    async def close(self) -> None
    async def status(self) -> AdGuardStatus
    async def list_clients(self) -> list[ClientState]
    async def snapshot(self) -> StateSnapshot   # groups (services+clients) using the state model above
    async def set_service(self, group: str, service: str, state: ServiceState) -> None
    async def set_group(self, group: str, state: ServiceState) -> None
    async def set_client(self, client: str, state: ServiceState) -> None
    async def set_protection(self, enabled: bool) -> None
    async def list_rules(self) -> list[str]
    async def add_rule(self, rule: str) -> None      # GET-merge-PUT into user_rules
    async def remove_rule(self, rule: str) -> None
```
- `client.py`: thin async httpx wrapper over the AdGuard REST endpoints above (basic auth from settings). GET-merge-PUT helpers for client `blocked_services` and for `user_rules`.
- `fake.py`: in-memory AdGuard seeded from `config` (create a client per config client with default_blocked) implementing the SAME low-level surface `service.py` uses, so the service works identically offline. Fake `status()` → reachable=True, mode "fake", version "fake-adguard".
- `service.py` decides live vs fake in `build_adguard`/`connect`; both paths return the same view models. Never raise out of `set_*` for transient HTTP errors without a clear message; let exceptions propagate to callers (they audit ok=False).
- Only devices named in `config.clients` are "managed"; ignore other AdGuard clients for group snapshots (but `list_clients` may include a flag).

## MODULE B — `warden/rules/compiler.py`
Turn plain English into a `CompiledRule` (see schema.py). **Must work with no API key** via a deterministic fallback.
```python
class RuleCompiler:
    def __init__(self, settings: Settings, vocab: Vocabulary) -> None: ...
    async def compile(self, text: str) -> CompiledRule       # LLM if settings.anthropic_api_key set, else compile_fallback; on LLM error fall back
    def compile_fallback(self, text: str) -> CompiledRule    # deterministic parser
```
- **Fallback must correctly handle the seed rules and close variants:**
  - "At 8pm every day, disable all kids devices" → ScheduleTrigger(cron `0 20 * * *`) + SetGroupAction(group=kids, state=blocked). "all <group> devices" / "all the kids' devices" ⇒ set_group.
  - "At 8pm every day, block YouTube for the kids" → ScheduleTrigger(`0 20 * * *`) + SetServiceAction(group=kids, service=youtube, state=blocked).
  - "When Luke has completed his Atom Learning, allow YouTube for the kids" → SignalTrigger(signal=`atom.luke.daily_complete`, edge=becomes_true, comparator=is_true) + SetServiceAction(group=kids, service=youtube, state=allowed).
  - Parse times: "8pm", "20:00", "8:30am", "at 7", plus "every day"/"on weekdays"/"weekends"/"on mondays" → cron day-of-week. Use the `croniter` lib to validate the cron you emit.
  - allow/block verbs and group/service aliases are in `vocab` (allow_words/block_words, group_aliases, service aliases). Map "the kids"→kids, "youtube"→youtube, etc.
  - Signal triggers: map "when <subject> has (completed|finished|done) [his/her] atom learning" → the `atom.<subject>.daily_complete` signal by matching the subject id and the signal whose `source==atom` and key endswith `daily_complete`. Generalise: search `vocab.signals` for a signal mentioning the subject + keywords.
  - Set `summary` to a clean human echo, `confidence` (1.0 fallback exact match, lower on guesses), and `warnings` for anything unresolved. If NOTHING parses (no action or no trigger), raise `ValueError("could not parse: ...")`.
- **Every exchange is journalled, verbatim.** `compiler.journal` is wired in main.py to `ctx.record_llm`; each call path (compile via SDK, compile via CLI, `complete_json` for the evaluator/reminders) records the system prompt, the message sent, the reply received, model, backend, tokens, duration and any error — in a `finally`, so the failures are recorded too. `complete_json(..., purpose=...)` names what the exchange was for (`rule_eval:<id>`, `reminders`, …); a malformed-JSON retry is journalled separately as `… (retry)`. `documents.py` makes the one call that doesn't go through the compiler and records itself the same way (page images named and sized in place of their base64). This is not optional bookkeeping: `compile()` swallows LLM errors and quietly uses the parser instead, so without the journal a bad compile leaves no trace at all. Recording must never break the call it describes — every failure inside it is logged and swallowed.
- **LLM path:** use the `anthropic` SDK (`AsyncAnthropic`), model `settings.llm_model`. System prompt embeds `vocab.prompt_reference()` and the JSON shape of CompiledRule; ask for JSON only; validate with `CompiledRule.model_validate`. On any error, return `compile_fallback(text)`. Keep prompt + schema in this file. Correct current model ids: opus `claude-opus-4-8`, sonnet `claude-sonnet-5`, haiku `claude-haiku-4-5-20251001` — the default in settings is haiku (cheap/fast). Do NOT hardcode a different id.

## MODULE C — `warden/rules/engine.py`
```python
class RuleEngine:
    def __init__(self, ctx: AppContext) -> None: ...
    async def start(self) -> None      # load enabled rules from ctx.db; schedule cron triggers on an AsyncIOScheduler(timezone=ctx.settings.tz); subscribe to ctx.bus for signal triggers
    async def stop(self) -> None
    async def reload_rules(self) -> None   # called after any rule create/update/delete/toggle
    async def run_rule(self, rule: Rule, reason: str, dry: bool = False) -> dict   # {ok, detail, actions:[...]}; evaluate conditions, then execute actions
```
- Executing an action maps kind→ctx.adguard method (set_service/set_group/set_client/set_protection/add_rule/remove_rule). After each successful rule run: `await ctx.audit(f"rule:{rule.id}", "rule_fired", rule.text, detail)`, `ctx.db.touch_rule(...)`, `await ctx.after_change("rule")`. On `dry=True`, compute + return the actions WITHOUT calling adguard.
- Signal handler (subscribed to bus): for each enabled rule whose trigger is a SignalTrigger matching the emitted signal key AND the edge/comparator (use the `edge` string the bus returns + the signal value vs comparator), call `run_rule(rule, reason="signal:<key>")`. Must be robust to signals with no matching rule.
- Use `croniter`/APScheduler CronTrigger.from_crontab for scheduling. Guard against a rule with `compiled is None` or unknown group/service (audit ok=False, don't crash the scheduler).

## MODULE D — `warden/adapters/registry.py`  +  the two adapters
**`registry.py`**
```python
class SourceRegistry:
    def __init__(self, ctx: AppContext) -> None: ...
    async def start(self) -> None    # discover adapters, schedule enabled sources per source.schedule (AsyncIOScheduler, tz). Do NOT auto-run on boot.
    async def stop(self) -> None
    def list_sources(self) -> list[dict]   # per source: key, display_name, adapter, enabled, schedule, subject, manifest(dict), last_run(dict|None from db), next_run(iso|None)
    async def run_source(self, key: str, live: bool | None = None, scheduled: bool = False) -> SourceRun   # collect_now, THEN evaluate dynamic rules; scheduled=True (the cron tick) skips the scrape when a rule's pre-eval collection already covered this tick
    async def collect_now(self, key: str, live: bool | None = None) -> SourceRun  # collect+persist only; joins an in-flight run; never calls the evaluator
    async def ensure_fresh(self, timeout: float | None = None) -> list[str]       # the barrier the evaluator awaits
```
- Discovery: for each `config.sources`, import `warden.adapters.<source.adapter>.adapter` and instantiate `Adapter()`. Build `SourceContext` via `resolve_secrets(source)` + `source.options` + `source.subject`; `fixtures_dir = warden/adapters/<adapter>/fixtures`; `since_cursor = ctx.db.get_kv(f"cursor:{key}")`. Decide `live`: if `live` param given use it; else `live = ctx has secrets AND playwright browser importable/available` else False (fixtures).
- Collection (`collect_now` → `_collect_and_store`): `ctx.db.start_run(key)`; `session=await adapter.authenticate(sctx)`; `res=await adapter.collect(sctx, session)`; `ctx.db.save_items(res.items)`; for each `res.signals`: `await ctx.bus.emit(sig)` (this drives the engine) and `await ctx.emit_signal_update(sig.key)`; store `res.next_cursor` to kv; read documents; store `state:<key>` + `record_state_snapshot` (its True/False becomes `SourceRun.state_changed`); `ctx.db.finish_run(...)`; `await ctx.audit("source:"+key,"source_run",...)`; `await ctx.after_change("source")`. Catch exceptions → finish_run ok=False, audit ok=False, return the SourceRun.
- **Collection completes before rules are evaluated — never concurrently.** Dynamic rules read the stored `state:<key>`, so an evaluation overlapping a collection judges the previous cycle's data and acts a whole cadence late. Two mechanisms hold the order: `run_source` evaluates only after `collect_now` has returned, and `RuleEvaluator` awaits `ensure_fresh()` before building the world. Concurrent callers share ONE in-flight collection per source (`_inflight`), so a cron tick, a waking rule and a "run now" landing together can't triple-scrape a shared session.
- `ensure_fresh` waits on any in-flight collection and starts one for any source whose scheduled poll is due (most recent cron fire time vs. the last run *attempted*, so a failing source isn't retried by every evaluation). Bounded by `WARDEN_COLLECT_WAIT_SEC` per source; on expiry, evaluate on the stored data and leave the collection running.
- After a successful run, `run_source` calls `evaluator.evaluate_all(f"source:{key}", force=<facts changed since the last sweep>)`. The flag (`_changed_since_eval`) is set by whichever collection archives a changed snapshot and consumed by the next sweep — needed because the collection that observes a change (often a rule's pre-eval one) is not the path that evaluates the other rules. `force` is what lets genuinely new facts wake a rule that is asleep until its own `next_check_at`; an identical poll (only the clock moved) must NOT wake it.

**`warden/adapters/atom/{__init__.py,adapter.py,manifest.json,fixtures/}`** — Atom Learning.
- `class Adapter(SourceAdapter)`; manifest id `atom`, emits_signals `["atom.<subject>.daily_complete","atom.<subject>.minutes_today"]` (fill subject at runtime from ctx.subject).
- **live** (`ctx.live and ctx.has_secrets`): use Playwright (async) to headless-login at `options.base_url` (Atom Learning — `app.atomlearning.com`), read today's activity/progress, produce Items (kind "progress") and derive the two signals (daily_complete bool from whether today's assigned work is done; minutes_today number). Wrap Playwright import inside the method so the module imports even where Playwright browsers aren't installed. On any live failure, fall back to fixtures + a warning.
- **fixtures** (default offline): read `fixtures/today.json` (you create a realistic sample: a child's daily tasks with completion + minutes) and produce the same Items + Signals from it. This is the demo path — make it produce `daily_complete=true` (or expose it so the UI/rule demo can show a transition). Subject id defaults to `ctx.subject or "luke"`; signal keys use that id.
- healthcheck → ok in fixtures mode; in live mode a cheap login check.
- Keep the collector faithful (contract §: mechanical, stable external_id = hash of source+date+task).

**`warden/adapters/weduc/{__init__.py,adapter.py,manifest.json,fixtures/}`** — school portal (see `../docs/weduclukedigestHANDOVER (2).md` and `../docs/sourceadapterCONTRACT.md`).
- Same shape. Fixtures-first (recon-only; no live creds by default). Produce a few realistic Items (newsfeed post, a form with due date, a calendar event) tagged with `audience_tags` (e.g. "Jellyfish Class","whole-school") and `subject_ids`. Derive signal `weduc.<subject>.forms_outstanding` (number) from the count of outstanding forms in fixtures. This adapter proves the "add a source = add an adapter, core unchanged" claim.

## MODULE E — `warden/api/{routes_state.py,routes_rules.py,routes_sources.py,routes_actions.py,ws.py}`
FastAPI routers. Each file: `router = APIRouter()`. Access services via `request.app.state.ctx`. Return pydantic models/dicts (FastAPI serialises). **Wrap every AdGuard mutation** with `await ctx.audit(...)` then `await ctx.after_change(...)` as described. Endpoints (paths are AFTER the `/api` prefix added in main.py, except ws):

`routes_state.py`
- `GET /state` → `ctx.adguard.snapshot()` (StateSnapshot)
- `GET /status` → `ctx.adguard.status()`
- `GET /config` → a dict summary: groups (name,tag,services), services, subjects, sources (key,display_name,enabled,schedule,subject), signals (from ctx.config) — for the UI to render controls dynamically.
- `GET /audit?limit=100` → `ctx.db.list_audit(limit)`. An entry's `ref` points at a bigger record the line summarises — `"llm:42"` is an exchange with the model.
- `GET /llm?limit=50` → recent LLM exchanges, newest first, **without** their bodies (character counts instead — a listing must not ship a megabyte of prompts).
- `GET /llm/{id}` → one exchange in full (system prompt, message sent, reply received, model/backend/tokens/ms/error), 404 once trimmed. Written by `ctx.record_llm`, which stores the row (`db.add_llm_call`, keeping the last `LLM_LOG_KEEP`) and then audits it as actor `llm`, action `llm_call`, `ref="llm:<id>"` — that is the line the UI hangs the transcript button off.

`routes_actions.py`  (actor="user")
- `POST /actions/service` `{group,service,state}` → set_service, audit, after_change, return snapshot
- `POST /actions/group` `{group,state}` → set_group ... return snapshot
- `POST /actions/client` `{client,state}` → set_client ... return snapshot
- **A dynamic rule's own `next_check_at` is authoritative.** Three paths can ask for a sweep: the safety-net tick (`WARDEN_EVAL_INTERVAL_MIN`, takes what `is_due`), the rule's own armed wake-up (`force=True`, takes it regardless), and a source collection that changed something (`on_data_change=True`). The last one is NOT a blanket force: it may pull a rule forward only inside `RuleEvaluator.EARLY_WAKE_WINDOW` (2h) — the rule that is WAITING on that data is checking hourly anyway, while a rule that has just said "he's on track, nothing more until tomorrow 4pm" has answered its question and must be left alone. It used to force everything, so such a rule was re-evaluated on every poll, disobeying its own text at ~22k tokens a time. `scripts/test_wake_semantics.py` guards this (no server, no LLM).
- Manual mutations (`/actions/service`, `/actions/group`, quick actions, `/hosts/services/{sid}` under reserved group `_host`) record a **switch pin** (`db.switch_pins`) BEFORE the write (rolled back on failure) so the WS push carries the hold. The dynamic evaluator must not change a pinned switch or host service (`evaluator.apply` skips it / expands set_group around it per-service, auditing what it withheld); SIGNAL-fired engine rules spare pinned targets too (`_execute(fired_by="signal")`). A schedule- or manually-run rule is the RESET: it clears pins BEFORE writing (a transient AdGuard error must not strand a pin past the reset moment) and applies regardless. The quick-action timed revert skips + keeps any pin newer than its arming. `/actions/client` records a `DeviceOverride` instead (pins have no device dimension). Fallback expiry next 04:00 local. `GET /state`, the WS priming message and the `state` event carry `holds` ("group/service" or "_host/service" → info) for the UI badges.
- `POST /actions/protection` `{enabled, reason?, device?}` → set_protection, audit, after_change, return status. Disabling REQUIRES `reason` (≥8 chars after cleaning, refused with a 422 naming the matched word if it reads like a kids-streaming excuse — per-service switches exist for that) and `device` (browser-remembered self-declared name). Refused attempts are audited as `protection_refused` (ok=False). User fragments are sanitised (`_clean_fragment`: quotes/em-dash/`reason:` stripped) so the audit grammar `DISABLED by <who> — reason: "…"` can't be forged; X-Forwarded-For is deliberately ignored (no proxy exists — it would be a free impersonation slot). Success is logged at WARNING. Re-enabling needs no reason.
- `GET /adguard/rules` → `{rules, enabled}` ; `POST /adguard/rules` `{rule}` add ; `DELETE /adguard/rules` `{rule}` remove (audit + after_change)

`routes_rules.py`
- `GET /rules` → `ctx.db.list_rules()`
- `POST /rules/preview` `{text}` → `await ctx.compiler.compile(text)` (CompiledRule; catch ValueError → 400 with message)
- `POST /rules` `{text, enabled?=true}` → create: new id (uuid4 hex[:8] or slug), compile (store compile_error on failure), upsert, `await ctx.engine.reload_rules()`, return Rule
- `PUT /rules/{id}` `{text?, enabled?}` → update; if text changed recompile; upsert; reload_rules; return Rule
- `POST /rules/{id}/toggle` → flip enabled; reload_rules; return Rule
- `DELETE /rules/{id}` → delete; reload_rules
- `POST /rules/{id}/run` `{dry?=false}` → `ctx.engine.run_rule(rule, "manual", dry)` → result dict

`routes_sources.py`
- `GET /sources` → `ctx.registry.list_sources()`
- `POST /sources/{key}/run?live=false` → `await ctx.registry.run_source(key, live)` → SourceRun
- `GET /sources/{key}/items?limit=50` → `ctx.db.recent_items(key, limit)`
- `GET /signals` → `ctx.bus.all()`
- `GET /signals/{key}/history?limit=50` → `ctx.bus.history(key, limit)`
- `POST /signals/emit` `{key,value}` → build a Signal (look up type from ctx.config.signals; default bool), `await ctx.bus.emit(sig)`, `await ctx.emit_signal_update(key)`, return the Signal. (This lets the UI simulate "Luke finished Atom" to demo a rule firing.)

`routes_reminders.py`
- `GET /reminders` → `await ctx.reminders.build()` — the Reminders page payload (school days, dated events, outstanding forms, parent actions, newsletter digest). Read-only and built from ALREADY-COLLECTED data: it never scrapes, so it must stay fast enough to serve on tab switch.
- `POST /reminders/refresh` → `build(force=True)`; re-reads messages with the LLM instead of reusing the cached reading. Costs one LLM call, so it is user-triggered only.

`ws.py`
- `@router.websocket("/ws")`: accept; `q = ctx.events.subscribe()`; immediately send `{"type":"state","snapshot":<snapshot>}` and `{"type":"signals","signals":[...]}`; then loop forwarding `await q.get()` as JSON; on disconnect `ctx.events.unsubscribe(q)`. Handle WebSocketDisconnect cleanly.

**`warden/adapters/parentpay/{__init__.py,adapter.py,login.py,manifest.json,fixtures/}`** — club bookings.
- **No ParentPay secret exists.** `login.py` mints a one-shot partner-login token from the WEDUC session (`GET app.weduc.co.uk/payment/parentpay/index/entity/{entity}/user/{child}`, scrape the `#/partnerlogin?token=…` URL), then exchanges it in Playwright because the token is in the URL FRAGMENT and only the SPA can read it. The resulting cookies are replayed with httpx. If the Weduc session is dead, it is refreshed via `weduc.login.capture_session` first, so the chain self-heals with no human.
- `adapter.py` uses the JSON API, NOT the `.aspx` page (that page is an Angular shell and parses to nothing): `internal-details/{club}/{consumer}` → `internalMemberId` (a different number from the ConsumerId in the URL), then `club-booking-calendar/{club}/{member}` → `sessions[]`.
- `bookingCutOffSettings` differs PER CLUB (`DayBeforeTimeBefore` vs `TheDayOfBefore`), so the deadline is computed per session. An unrecognised scheme must return "" — never a guessed deadline.
- Payment items come from `/V3Payer4W3/Home/PaymentItems/PaymentItems.aspx` — server-rendered HTML, the only HTML this adapter parses. Anchor on `rptPaymentItems_PaymentItem_N` ids, not layout classes. `Payment due` + amount = an outstanding charge; `Balance` = a running account and NEVER an action; `Paid` = settled.
- **STRICTLY READ-ONLY.** The same API books, cancels and pays. Only ever issue GETs to the two endpoints above.

## MODULE G — `warden/reminders.py`
`class ReminderBuilder(ctx)` with `async def build(self, force: bool = False) -> dict`. Assigned to `ctx.reminders` in main.py. Interprets stored Weduc data for the Reminders tab; the adapter stays mechanical (contract §2), so all relevance/date reasoning lives here.

- **Deterministic spine first.** Term/closure days from `state:weduc`→`school`, dated events from `calendar_event` Items, forms from `forms_outstanding`, newsletter `key_dates`/`actions_for_parents`. This path must work with NO LLM backend — `WARDEN_LLM=off` gives a thinner page, never an empty or wrong one.
- **`build(force=False, wait=True)`; the GET passes `wait=False` and NEVER blocks on the model.** A cold reading takes 15-20s, and waiting for it left the page on "Loading…" showing none of the spine, which needs no model at all. With `wait=False` an unread message pool returns `llm.pending` immediately and `_start_background_read()` does the reading off-request, caches it, and publishes a `{"type": "reminders"}` event; the SPA reloads the page on that (with a 10s fallback poll while pending) and the next GET is a cache hit. One read at a time (`self._reading`), and a failed read is remembered per cache key for `_RETRY_FAILED_READ_AFTER` — otherwise every reload of a pending page starts another doomed call. `POST /reminders/refresh` still waits: it is a button press with its own state.
- **The LLM pass is additive and never fatal.** It reads `message`/`post` Items from the last 30 days via `ctx.compiler.complete_json`, each labelled with its own SENT date so "tomorrow"/"on Friday" resolve correctly. Any exception is caught and reported in the payload's `llm.reason`.
- **The model proposes, `_verify` disposes** — same posture as `documents.py`. A reminder survives only if it cites an id that was in the input, its `quote` appears verbatim in that message, its date is not past, and its `for_class` is not another class. Rejects go to `unverified` with a `_rejected` reason. Never relax this: the page's whole value is that a parent can trust a line without checking it.
- Class relevance is derived from the portal's own `audience_tags` (plus config `subjects[].contexts`), never a hard-coded roster. The child's class comes from the LIVE portal value first, config contexts only as the offline fallback.
- Readings cache in kv under `reminders:weduc:<hash of the messages + date + class>`, so an unchanged re-poll costs nothing.

## MODULE F — `warden/web/{index.html, static/css/app.css, static/js/app.js}`
A polished, **no-build** single-page app in **vanilla JS** (no framework, no CDN — must work fully offline in a container). Dark, modern, card-based. Talks to `/api/*`; opens `/ws` for live updates (fallback to polling `/api/state` every 5s if WS drops).

Tabs / views:
- **Dashboard** — AdGuard status pill (mode live/fake, version, protection toggle). For each group: a card with a toggle per service (from `/api/config` + `/api/state`), an "all off (bedtime)" button (calls `/actions/group` blocked) and "all on". Toggles call `/actions/service`. Reflect live WS state pushes.
- **Rules** — list rules with their English text, a compiled-summary line, enabled toggle, Run-now, Delete. An "Add rule" box: type English → shows live compiled preview (`/rules/preview`) → Save (`/rules`). Show compile_error/warnings clearly. Show trigger type (⏰ schedule / ⚡ signal) and last_fired.
- **Sources** — card per source: name, adapter, enabled, schedule (cron, human-hint), subject, last run (items/signals/ok), "Run now" (fixtures) button, and recent items list (`/sources/{key}/items`). A **Signals** panel: each known signal, current value, and a "simulate" control (`/signals/emit`) — e.g. flip `atom.luke.daily_complete` true to watch the YouTube rule fire live in the Activity feed and Dashboard.
- **Activity** — live audit log (from WS `audit` events + initial `/api/audit`): time, actor, action, target, ok. A row whose `ref` is `llm:<id>` gets a **prompt ⤢** button that opens the exchange in full (`GET /api/llm/<id>`) — system prompt, what was sent, what came back, each copyable. Model calls are the noisiest lines in the feed, so the header carries an "LLM calls" checkbox to hide them; it defaults to ON, because being able to see what Warden asked and what it was told is the point of keeping them.
- Small **Settings/About** note: AdGuard URL + mode, LLM on/off, that secrets live in .env.
- Design tokens in CSS (`:root` vars), responsive, no external fonts (system stack), accessible toggles. Put an inline SVG logo/wordmark "Warden". Keep JS in one well-structured `app.js` (a tiny store + render functions + a WS client). It's fine to be a few hundred lines; make it clean and commented.
- The API returns `state` values as the string "allowed"/"blocked" (ServiceState). Treat `allowed`=on.

---
When done, from `warden/` run `.venv/Scripts/python.exe -c "import warden.<your top module>"` to confirm it imports. Report: files created, any interface point you were unsure about, and anything you stubbed.
