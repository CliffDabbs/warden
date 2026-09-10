# Warden — interface reference

What Warden exposes and what it expects: the HTTP and WebSocket surface, the compiled-rule
AST, the source-adapter contract, the AdGuard and host-control drivers, every configuration
key and every environment variable, and what is on disk.

This is a **reference** — look things up in it. For what Warden is and why it is shaped this
way, read [README.md](README.md); for running it, [USER-GUIDE.md](USER-GUIDE.md).

> The document that used to live here was a *build brief*: instructions written before the
> code existed, to hand each module to a separate agent to implement in parallel. It is
> kept,
> unedited and clearly labelled, at
> [`../docs/INTERFACES-build-brief.md`](../docs/INTERFACES-build-brief.md) — several of its
> seams are still load-bearing and the reasoning behind them is only recorded there.

## Contents

- [HTTP routes](#http-routes) — every endpoint, request and response
- [Live updates](#live-updates) and [Authentication](#authentication)
- [Compiled rules](#compiled-rules) — the AST, the two execution paths, the wake-up contract
- [Writing a source adapter](#writing-a-source-adapter) — the extension point
- [AdGuard](#adguard) — REST surface, the service-state model, group membership
- [Host control](#host-control) — the SSH lever
- [Configuration](#configuration) — `config/warden.yaml` and the environment
- [Persistence](#persistence) — tables, kv namespaces, retention

---

## HTTP routes

Every router in `warden/api/` is mounted under `/api` by `main.py` except `ws.py`, which is
included with no prefix. The four routes defined inline in `main.py` (`/`, `/login`,
`/logout`, `/healthz`) also carry no prefix. So the only paths without `/api` are `/`,
`/login`, `/logout`, `/healthz`, `/ws` and the `/static` mount.

Two cross-cutting behaviours apply everywhere and are not repeated per row:

- **401** — the auth middleware (`warden/auth.py`) wraps the whole app. When
  `WARDEN_PASSWORD` is set and the `warden_session` cookie is missing or bad, any path
  starting `/api/` and `/ws` gets `401 {"detail": "authentication required"}`; any other
  path gets a `303` to `/login`. `/login`, `/logout`, `/healthz`, `/favicon.ico` and
  `/static/*` are exempt. With no password configured the middleware is a no-op.
- **422** — FastAPI's own body/query validation, on every route that takes a pydantic body.

### State and config — `api/routes_state.py`

All read-only; nothing here mutates AdGuard, so none of them audit or push.

| Method | Path | Does | Body | Response | Non-200 |
|---|---|---|---|---|---|
| GET | `/api/state` | Full dashboard snapshot. | — | `StateSnapshot` fields flattened (`adguard`, `groups`, `updated_at`) plus `holds` — a dict keyed `"group/service"` → `{state, since, expires_at}`. `holds` rides alongside rather than inside the snapshot so the UI can badge manual flips the dynamic evaluator must not undo. | — |
| GET | `/api/status` | The AdGuard connection/protection pill. | — | `AdGuardStatus` — `reachable`, `mode` (`live`\|`fake`), `version`, `protection_enabled`, `url`, `detail`. | — |
| GET | `/api/config` | Static vocabulary the SPA draws controls from, all out of `config/warden.yaml`. | — | `{adguard:{url}, compiler:{backend, model, eval_model}, auth:{enabled}, groups:[{name, tag, services[]}], services[], subjects[], sources:[{key, display_name, enabled, schedule, subject}], signals[], quick_actions[]}`. `compiler.backend` is one of `anthropic-api`, `claude-cli`, `builtin-parser`; `eval_model` falls back to `llm_model`. | — |
| GET | `/api/audit` | Recent audit entries, newest first. | query `limit: int = 100` | `list[AuditEntry]` — `id, at, actor, action, target, detail, ok, ref`. `ref` points at a bigger record, e.g. `"llm:42"`. | — |
| GET | `/api/llm` | Recent LLM exchanges, newest first, **without** their bodies — a listing shouldn't ship a megabyte of prompts to draw a table. | query `limit: int = 50` | `list[LlmCall]` with sizes in place of text. | — |
| GET | `/api/llm/{call_id}` | One exchange verbatim: system prompt, message sent, reply received. | — | `LlmCall` — `id, at, purpose, backend, model, system, prompt, response, ok, ms, input_tokens, output_tokens, error`. | **404** — the log keeps only the recent few hundred calls; older ones are trimmed while their audit line remains. |

`routes_state.py` imports `StateSnapshot` but never uses it — dead import.

### Actions — `api/routes_actions.py`

Every route here mutates network state and goes through `_mutate`: perform, then
`ctx.audit(...)` and `ctx.after_change(...)`. On failure `_mutate` still audits `ok=False`
and refreshes the snapshot before raising, so the log and the UI reflect reality — hence the
uniform **502** rather than a 500.

Bodies: `ServiceBody{group: str, service: str, state: ServiceState}`, `GroupBody{group: str,
state: ServiceState}`, `ClientBody{client: str, state: ServiceState}`,
`ProtectionBody{enabled: bool, reason: str = "", device: str = ""}`. `ServiceState` is a str
enum, `allowed` | `blocked`. No field in any of these has a default except `reason` and
`device`.

| Method | Path | Does | Body | Response | Non-200 |
|---|---|---|---|---|---|
| POST | `/api/actions/service` | Flip one service for one group, and hold it against the evaluator until the next scheduled reset. | `ServiceBody` | `StateSnapshot` | **502** on AdGuard failure — the pin is taken *first* so the WS push carries the badge with the flip, and cleared again if the write fails. |
| POST | `/api/actions/group` | Flip every managed service for a group, holding each. | `GroupBody` | `StateSnapshot` | **502**, pins rolled back. |
| POST | `/api/actions/client` | Flip one device's managed services. `allowed` records a `DeviceOverride` (pins have no device dimension, so the exemption is what the group writer honours); `blocked` withdraws any standing one. | `ClientBody` | `StateSnapshot` | **502**, override rolled back on an unblock. |
| POST | `/api/actions/protection` | Whole-network protection on/off. | `ProtectionBody` | `AdGuardStatus` | **422** on three refusals when disabling — reason under 8 characters after cleaning, a reason matching `_STREAMING_EXCUSE` (per-service switches exist for exactly that), or no self-declared device name. Each refusal is itself audited as `protection_refused`, because someone iterating wordings until one slips past should be visible in the feed rather than silently 422'd. **502** on AdGuard failure. |

`_requester` identifies the caller from the self-declared device name plus the socket peer
address, deliberately **not** `X-Forwarded-For` — there is no proxy in this deployment, so
that header is a free impersonation slot. `_clean_fragment` strips quotes, em-dashes and
`reason:` from both halves so a device name cannot forge a reason.

### Quick actions — `api/routes_actions.py`

| Method | Path | Does | Body | Response | Non-200 |
|---|---|---|---|---|---|
| GET | `/api/actions/quick` | List configured presets with the concrete flips each resolves to. | — | `list[QuickActionConfig dumps]` each with `affects: [{group, service, state}]`. | — |
| POST | `/api/actions/quick/{aid}` | Fire a preset: expand its steps, apply each, pin them, optionally arm a timed revert. | — | `StateSnapshot` | **404** unknown `aid`; **400** when the action resolves to no valid switches. |

`_expand` fans a step naming only a `service` out to every group exposing it, and a step
naming a `group` out to that group's whole service list; unknown ids are skipped rather than
failing the action. Individual flip failures inside a run are audited `ok=False` but **do
not** fail the request — the response is still a 200 snapshot, and the audit detail lists
only what actually applied. When `revert_after_minutes` is set the affected switches are
snapshotted first, so the revert restores exactly what was there rather than imposing a
blanket baseline. Host-service steps are global and never auto-reverted.

### Devices — `api/routes_devices.py`

`UnblockBody{client: str, duration: str = "2h"}`; `duration` must be a key of `DURATIONS` =
`2h`, `4h`, `24h`, `forever` (`forever` = no expiry, stands until cancelled).

| Method | Path | Does | Body | Response | Non-200 |
|---|---|---|---|---|---|
| GET | `/api/devices` | Every client AdGuard knows about, managed or not, annotated with its override. | — | `{devices: DeviceInfo[], durations: ["2h","4h","24h","forever"]}` | — |
| POST | `/api/devices/unblock` | Exempt a device from Warden's group writes for a window, then clear its blocks. | `UnblockBody` | `{ok: true, client, expires_at}` (`expires_at` null for `forever`) | **400** unknown duration; **502** on AdGuard failure, with the override deleted again so no phantom exemption is left behind. |
| DELETE | `/api/devices/override` | Cancel an override early and put the blocks straight back. | `UnblockBody` (**required JSON body on a DELETE**) | `{ok: true, client}` | **404** no override for that client; **502** on AdGuard failure. |

The exemption is recorded *before* the unblock so a rule firing in the gap can't re-block
the device a moment after we cleared it.

### Hosts — `api/routes_actions.py`

`HostServiceBody{running: bool}`.

| Method | Path | Does | Body | Response | Non-200 |
|---|---|---|---|---|---|
| GET | `/api/hosts/services` | Managed services with live state. Probing each opens an SSH connection, so this is slower than the other reads. | — | `list[{id, name, description, icon, host, confirm, running, detail, configured}]`; `[]` when `ctx.hosts` is None. | — |
| POST | `/api/hosts/services/{sid}` | Start or stop a service over SSH, and pin it under `_host/{sid}` so the evaluator can't flip it back (movie night stays a movie night). | `HostServiceBody` | `{id, requested: "running"\|"stopped", ok, detail}` | **404** no such managed service; **502** on an exception from the SSH layer. |
| GET | `/api/hosts/{name}/discover` | Probe the box for `qpkg_cli`, `docker`, `systemd`, `init.d` — a QNAP may run Plex either way and the correct command differs, so look rather than guess in config. | — | `{uname, qpkg_cli, qpkg_list, docker, docker_ps, systemctl, initd}`, each `{exit, out, err}` or `{error}`; probing stops at the first connection error. | **404** no such host; **400** when SSH isn't available (`asyncssh` missing, or the host's user/password env vars unset). |

### Rules — `api/routes_rules.py`

Bodies: `PreviewBody{text: str}`, `CreateBody{text: str, enabled: bool = True}`,
`UpdateBody{text: Optional[str] = None, enabled: Optional[bool] = None}`, `RunBody{dry: bool
= False}`.

Create/update/toggle/recompile/delete all call `ctx.engine.reload_rules()` so the running
engine's schedule and signal subscriptions stay in sync with the store.

| Method | Path | Does | Body | Response | Non-200 |
|---|---|---|---|---|---|
| GET | `/api/rules` | All stored rules. | — | `list[Rule]` | — |
| POST | `/api/rules/preview` | Compile without persisting. | `PreviewBody` | `CompiledRule` — `trigger, conditions, actions, summary, confidence, warnings, dynamic`. | **400** on a genuine parse failure (`ValueError`); other compiler exceptions propagate as 500. |
| POST | `/api/rules` | Create a rule, storing the compiled AST or a `compile_error`. | `CreateBody` | `Rule` | — (compile failure is stored on the rule, not raised) |
| PUT | `/api/rules/{rid}` | Update text and/or enabled. Recompiles **only if the text actually changed**, and then drops `next_check_at`/`next_check_reason` — the text drives the cadence, so a rewrite invalidates the wake-up the old text asked for. | `UpdateBody` | `Rule` | **404** no such rule |
| POST | `/api/rules/{rid}/recompile` | Compile the stored text again without changing a word — PUT only recompiles on a text change, so a rule that compiled badly had no way back except editing it into something else and back again. Audits the outcome, `ok=False` if there is an error *or* any warning. | — | `Rule` | **404** |
| POST | `/api/rules/{rid}/toggle` | Flip `enabled`; disarms the pending next-check when switching off. | — | `Rule` | **404** |
| DELETE | `/api/rules/{rid}` | Delete a rule (no body). | — | `{ok: true, id}` | **404** |
| POST | `/api/rules/{rid}/run` | Fire by hand. Deterministic rules run their compiled actions; dynamic rules are evaluated live by the LLM against current world state. | `RunBody`, **optional** (`body: Optional[RunBody] = None`, so an empty request means `dry=False`) | Deterministic: `{ok, detail, actions:[…per-action ok/error]}`, or `{ok: true, skipped: true, detail, actions: []}` when conditions fail. Dynamic dry run: `{ok, dry, dynamic, condition_met, detail, actions, next_check_at, next_check_reason}` — the wake-up it *would* arm is the whole point of a self-scheduling rule, so a dry run has to show it. Dynamic live: `{ok, dynamic, detail, **evaluate_one result}`. | **404** unknown rule; **400** for a dynamic rule with no LLM backend. |

### Sources — `api/routes_sources.py`

`TargetBody{islands: int, week_start: Optional[str] = None, note: str = ""}` — `week_start`
is a Monday `YYYY-MM-DD`, defaulting to the current week.

| Method | Path | Does | Body | Response | Non-200 |
|---|---|---|---|---|---|
| GET | `/api/sources` | Project the registry: every configured source with manifest, last run, next run, error, live-vs-fixture data note. | — | `list[dict]` | — |
| POST | `/api/sources/{key}/run` | Collect one source now, then sweep the dynamic rules. | query `live: Optional[bool] = None` — `None` lets the registry decide (fixtures unless secrets and a browser are available). | `SourceRun` — `id, source, started_at, finished_at, ok, item_count, signal_count, detail, state_changed`. | **500** on an unknown key (`KeyError` from `collect_now`, not translated). A collection *failure* is not an error status — it comes back as a `SourceRun` with `ok=false`. |
| GET | `/api/sources/{key}/items` | Recent normalised items. | query `limit: int = 50` | `list[Item]` | — (an unknown key returns `[]`) |
| GET | `/api/sources/{key}/history` | Archived "state of play" snapshots, newest first, one per change. | query `limit: int = 200` | `list[dict]` | — |
| GET | `/api/sources/{key}/target` | This week's workload target plus today's share of it, derived from the weekly number rather than fixed. | query `week_start: Optional[str] = None` | `{week_start, islands, origin, note, set_at, published, today}`. `origin` is `override` \| `atom-published` \| `config-default` \| `unset`; `today` is `{islands_due_today, still_to_do_today, completed_today, completed_week_to_date, explain, as_of}` or null — read back from the adapter's stored state rather than recomputed, and suppressed when the snapshot is from a different week. | **404** unknown source |
| PUT | `/api/sources/{key}/target` | Set this week's target. Audits, pushes, and re-collects in the background so today's share, `islands_due_today` and `daily_complete` are recomputed at once instead of at the next hourly poll. | `TargetBody` | `{week_start, islands, origin: "override", note, set_at}` — note this one omits the `today` key the GET and DELETE include. | **404** unknown source; **400** `islands < 0`. |
| DELETE | `/api/sources/{key}/target` | Drop the override and go back to the source's own published plan — an override is a deliberate exception ("he's ill", "half term"), not the way to keep the number current. | query `week_start: Optional[str] = None` (**no body**) | Same shape as GET | **404** unknown source |
| GET | `/api/sources/{key}/documents` | What was read out of the source's attached newsletter PDFs; digests are cached by the portal's attachment id, so this doubles as the record of which newsletters have been read. | — | `{reader_available, reason, count, documents}` | — |

### Signals — `api/routes_sources.py`

`EmitBody{key: str, value: Any}`.

| Method | Path | Does | Body | Response | Non-200 |
|---|---|---|---|---|---|
| GET | `/api/signals` | Current value of every signal on the bus. | — | `list[Signal]` | — |
| GET | `/api/signals/{key}/history` | Past values of one signal, newest first. | query `limit: int = 50` | `list[Signal]` | — |
| POST | `/api/signals/emit` | Inject a signal, fanning out to the engine's signal-trigger subscribers and pushing the new value to WS clients. | `EmitBody` | `Signal` — `key, value, type, source, subject, at, meta`. | **500** if the config declares the signal `number` and the value won't parse as a float. An unknown key is not an error: it is treated as `bool` and emitted. |

The declared type in config coerces the incoming JSON value so a bool signal becomes a real
bool — essential for the bus's edge detection.

### Reminders — `api/routes_reminders.py`

Built entirely from data the Weduc/ParentPay adapters already collected; this never scrapes.

| Method | Path | Does | Body | Response | Non-200 |
|---|---|---|---|---|---|
| GET | `/api/reminders` | The whole Reminders page. Never waits on the model: with no cached reading for the messages in hand it returns at once with `llm.pending`, runs the reading in the background, and a `reminders` WS event says when it landed. | — | `{generated_at, today, tz, subject, school, days, forms, payments, alerts, clubs, actions, newsletter, data, llm, unverified}` | **503** when `ctx.reminders` is None (unreachable in practice — `build_app` always sets it) |
| POST | `/api/reminders/refresh` | Same payload, but re-reads the messages instead of reusing the cached reading. This one **does** wait — it is a button press with its own "Re-reading…" state, so the answer is what was asked for. | — | Same shape | **503**, same condition |

### AdGuard custom rules — `api/routes_actions.py`

`RuleBody{rule: str}` — AdGuard filtering syntax. All three share `_adguard_rules_payload`.

| Method | Path | Does | Body | Response | Non-200 |
|---|---|---|---|---|---|
| GET | `/api/adguard/rules` | List AdGuard's user filtering rules. | — | `{rules: list[str], enabled: bool}` — `enabled` is `status.protection_enabled`, i.e. whole-network protection, not the state of the rule list. | — |
| POST | `/api/adguard/rules` | Add one rule. | `RuleBody` | Same payload, re-read after the write. | **502** via `_mutate` |
| DELETE | `/api/adguard/rules` | Remove one rule. | `RuleBody` (**required JSON body on a DELETE**) | Same payload | **502** via `_mutate` |

### Non-`/api` routes — `main.py` and `api/ws.py`

| Method | Path | Does | Response | Non-200 |
|---|---|---|---|---|
| GET | `/` | The SPA shell, with `/static/css/app.css` and `/static/js/app.js` URLs stamped `?v=<mtime>`. Nothing sets `Cache-Control` on `/static`, so a browser guesses days for a file that hasn't changed in a while; stamping makes a changed file a different URL, so an upgrade lands the moment the container restarts. Served `Cache-Control: no-cache`. | HTML | **303** to `/login` when a password is set and the cookie is missing |
| GET | `/login` | Sign-in page. | HTML | **303** to `/` when already signed in |
| POST | `/login` | Check credentials from a urlencoded form (`username`, `password`, parsed by hand with `parse_qs`, not a pydantic body). | **303** to `/` plus the `warden_session` cookie — httponly, samesite=lax, `max_age` ~10 years | **401** with the login page re-rendered and an error |
| GET | `/logout` | Clear the cookie. | **303** to `/login` | — |
| GET | `/healthz` | Liveness. Public — exempt from the auth middleware. | `{ok: true, adguard_mode}` | — |
| WS | `/ws` | Live push. Subscribes to the EventHub, primes the client with `{type:"state", snapshot, holds}` (or `{type:"error", detail}` if the snapshot fails — a broken snapshot must not abort the connection) and `{type:"signals", signals}`, then forwards every published event as JSON. Producers are `ctx.after_change` (`type:"state"`), `ctx.audit` (`type:"audit"`) and `ctx.emit_signal_update` (`type:"signal"`). | JSON frames | **close 1008** when the cookie check fails — HTTP middleware doesn't see websockets, so the gate is enforced inside the handler |

### Call-outs

**DELETE with a required JSON body** — two routes, both of which will 422 on an empty
DELETE:

- `DELETE /api/devices/override` takes `UnblockBody`. Only `client` is read; `duration` is
  accepted and ignored (the SPA sends `{client, duration: "2h"}` anyway).
- `DELETE /api/adguard/rules` takes `RuleBody`.

`DELETE /api/rules/{rid}` and `DELETE /api/sources/{key}/target` take no body — the latter
uses a `week_start` query param.

**Deliberate "200 with ok:false"** rather than an error status:

- `POST /api/rules/{rid}/run` on a dynamic rule whose evaluation raises: returns `{ok:
  false, dynamic: true, applied: [], detail: "evaluation failed: …"}` after auditing. An
  evaluation failure is information, not a server fault — a 500 gave the UI an unparseable
  body and told the operator nothing.
- `POST /api/rules/{rid}/run` on an uncompiled rule, and on one whose conditions don't hold
  (`{ok: true, skipped: true}` — a rule that "wouldn't run" is a successful answer).
- `POST /api/hosts/services/{sid}` when the SSH command runs but exits non-zero: `{ok:
  false, detail}`. Only a transport-level exception is a 502.
- `POST /api/sources/{key}/run` when collection fails: a `SourceRun` with `ok=false`.
  `_collect_and_store` captures every failure into a failed run and never raises, so a
  scheduler tick can't crash the loop — and the manual route inherits that.
- `POST /api/actions/quick/{aid}` when some flips fail: still a 200 snapshot, with the
  failures in the audit log only.

**Routes with no caller in the SPA** (`web/static/js/app.js` reaches the API only through
the `API` wrapper at line 155, base `/api`):

| Route | Note |
|---|---|
| `GET /api/llm` | The Activity feed shows LLM lines from `/audit` and opens transcripts via `/llm/{id}`; the listing endpoint is unused. |
| `GET /api/actions/quick` | The dashboard builds its buttons from `config.quick_actions` instead, so the `affects` expansion this route computes is never displayed. |
| `POST /api/actions/client` | Per-device flips in the UI go through `/devices/unblock` and `/devices/override`. The engine's `set_client` action calls `ctx.adguard.set_client` directly, not this route — so it is API-only surface. |
| `GET`/`POST`/`DELETE /api/adguard/rules` | The whole custom-rules area is unreachable from the UI; only compiled `add_rule`/`remove_rule` rule actions exercise the underlying service methods. |
| `GET /api/sources/{key}/history` | No caller. |
| `GET /api/sources/{key}/documents` | No caller — the newsletter digests surface through `/api/reminders` instead. |
| `GET /api/signals/{key}/history` | No caller. |
| `GET /api/hosts/{name}/discover` | A first-run helper, run by hand. |


---

## Live updates

Warden pushes state to the browser over a single WebSocket at `/ws` (mounted with no `/api`
prefix). The socket is one-way: the server never calls `receive`, and the client never sends
— a client that goes away is noticed on the next send, which raises and drops through to
cleanup.

### Handshake

1. The endpoint checks the session cookie itself, before `accept()` — HTTP middleware never
   sees a WebSocket scope, so the gate has to live in the endpoint. A bad or missing cookie
   gets `close(code=1008)` (policy violation) with no accept.
2. `accept()`, then `ctx.events.subscribe()` — a fresh queue on the `EventHub`.
3. Priming messages, in order:
   - `state` — `await ctx.adguard.snapshot()` plus `_holds(ctx)`. If the snapshot raises, an
     `error` message is sent instead and the connection stays up; a broken AdGuard must not
     cost you the live link.
   - `signals` — every signal currently in the bus (`ctx.bus.all()`).
4. Then a loop: `await q.get()` → `send_json(event)`, forwarding hub events verbatim until
   the socket closes. `unsubscribe(q)` runs in `finally`.

Client side (`web/static/js/app.js`): connects to `ws(s)://<host>/ws`, reconnects with a 1s
backoff multiplied by 1.7 up to 15s, and while the socket is down polls `GET /api/state`
every 5s.

### Event types

Every message is a JSON object with a `type`. These are all of them.

| `type` | Published by | Payload |
|---|---|---|
| `state` | `ws.py` priming; `ctx.after_change(reason)` | `snapshot` (`StateSnapshot`), `holds`, plus `reason: str` — only on the `after_change` copy; the priming message has no `reason` |
| `signals` | `ws.py` priming only | `signals`: array of `Signal` |
| `signal` | `ctx.emit_signal_update(key)` | `signal`: one `Signal` |
| `audit` | `ctx.audit(...)` | `entry`: one `AuditEntry` |
| `reminders` | `ReminderBuilder`, when a background message read finishes | `reason: "message reading finished"` — no data; a nudge to refetch |
| `error` | `ws.py` priming failure; `after_change` failure | `detail: "snapshot failed: <exc>"` |

`signals` (plural) is never published to the hub — it is only ever the priming message, so a
client sees it exactly once per connection.

Payload shapes:

- **`snapshot`** — `adguard` (`reachable`, `mode` `live|fake`, `version`,
  `protection_enabled`, `url`, `detail`), `groups[]` (`name`, `tag`, `services[]` of
  `ServiceToggle`, `clients[]` of `ClientState`, `all_blocked`), `updated_at`.
- **`holds`** — a map keyed `"<group>/<service>"` → `{state, since, expires_at}`, built from
  the active pins. Identical shape in the priming message and in `after_change`.
- **`Signal`** — `key`, `value` (bool | number | string), `type`, `source`, `subject`, `at`,
  `meta`.
- **`AuditEntry`** — `id`, `at`, `actor`, `action`, `target`, `detail`, `ok`, `ref` (e.g.
  `llm:42`, a pointer to the stored transcript).

### What the client does with each

| `type` | Handling |
|---|---|
| `state` | Replaces `store.holds` when present, applies the snapshot, re-renders the dashboard |
| `signals` | Upserts by key; re-renders only if the Sources view is open |
| `signal` | Upserts, patches the weekly-target "today" figure from the signal rather than refetching the target, patches the value in place on Sources so simulate inputs survive |
| `audit` | Prepends to the feed, capped at 300 rows |
| `reminders` | Reloads the Reminders view, and only if it is the one on screen |
| `error` | Toast |

The `switch` has no `default` and the `JSON.parse` is wrapped in a bare `try`/`catch`, so an
unknown type or malformed frame is silently ignored.

### Slow clients

`EventHub.subscribe()` hands out an `asyncio.Queue(maxsize=100)` per socket. `publish()`
walks a copy of the subscriber set and uses `put_nowait`; on `QueueFull` it swallows the
event — drop rather than block, since a producer here is an action or the rules engine and
blocking it on a stalled browser would stall the house.

Drops are per-client and silent: there is no sequence number, ack or backfill. A dropped
`state` repairs itself, because the next one carries a whole snapshot. A dropped `audit` or
`signal` is gone for that client until it reloads the view (the Activity view refetches `GET
/api/audit?limit=100`).

The WebSocket endpoint is the only subscriber to the hub.

## Authentication

Off unless a password is set, so Warden runs open out of the box. Everything is stdlib
`hmac`/`hashlib` — no dependency, no session store.

| Setting | Env | Default |
|---|---|---|
| `auth_username` | `WARDEN_USERNAME` | `admin` |
| `auth_password` | `WARDEN_PASSWORD` | `""` — empty means auth disabled |
| `auth_secret` | `WARDEN_SECRET` | `""` — derived from the credentials if unset |

`Auth.enabled` is `bool(password)`. The middleware is a no-op and `check_cookies` returns
`True` whenever it is false.

### Cookie

- Name: `warden_session`.
- Value: `<username>.<hmac-sha256 hexdigest>`, the HMAC taken over `"warden|" + username`
  keyed by `sha256(WARDEN_SECRET or "<username>:<password>")`.
- The cookie carries no secret, only a signature over the username; it is verified by
  recomputing and `hmac.compare_digest`, and the embedded username must equal the configured
  one.
- Set on a successful `POST /login`: `max_age=10*365*24*3600` (~10 years), `httponly`,
  `samesite="lax"`, `path="/"`. No `Secure` flag — it has to work over plain HTTP on the
  LAN.
- The token holds no timestamp or nonce, so nothing expires it server-side; the ~10-year
  max-age is the whole lifetime, deliberately "practically forever" so a reboot never logs
  the household out.

### Public paths

Exact: `/login`, `/logout`, `/healthz`, `/favicon.ico`. Prefix: `/static/`.

### Unauthenticated behaviour

| Path | Response |
|---|---|
| `/api/*` or exactly `/ws` | `401` with `{"detail": "authentication required"}` |
| Everything else | `303` redirect to `/login` |

The split exists so a fetch from the SPA gets a status it can act on instead of a login
page's HTML.

The `path == "/ws"` branch is close to dead: `make_auth_middleware` is installed with
`app.middleware("http")`, which never runs for a WebSocket scope. It fires only for a plain
HTTP request to `/ws` (a `curl` with no upgrade headers). Real handshakes are gated inside
the endpoint and get close code `1008`.

### Other routes

- `GET /login` with a valid cookie redirects to `/` (303). With auth disabled
  `check_cookies` is always true, so `/login` always redirects and `check_credentials`
  always returns false — the login form is unusable, correctly, since nothing needs it.
- `POST /login` with wrong credentials re-renders the form with status `401`.
- `GET /logout` deletes the cookie and redirects to `/login`. It only clears the browser's
  copy — the token stays valid, as there is no server-side revocation, so a cookie captured
  elsewhere survives a logout.

### What invalidates existing cookies

The signing secret is `sha256(WARDEN_SECRET)` if set, otherwise
`sha256("<username>:<password>")`. So:

- Changing `WARDEN_PASSWORD` or `WARDEN_USERNAME` while `WARDEN_SECRET` is unset rotates the
  derived secret and invalidates every issued cookie.
- Changing, setting, or unsetting `WARDEN_SECRET` does the same.
- Setting `WARDEN_SECRET` explicitly pins the secret, after which a password change no
  longer invalidates anything.

Nothing else does — not a restart, not a redeploy, not logout.


---

## Compiled rules

A rule is `WHEN <trigger> [IF <conditions>] THEN <actions>`. The compiler turns one
plain-English instruction into a `CompiledRule`; the engine runs it. The types in
`warden/rules/schema.py` are frozen and JSON-round-trippable — they are persisted in SQLite
as the rule's `compiled` blob, so a shape change breaks stored rules.

Two execution paths exist, and they are mutually exclusive per rule:

| Path | Owner | Fires on | Rules it touches |
|---|---|---|---|
| Deterministic | `RuleEngine` (`engine.py`) | APScheduler cron job, or a `SignalBus` emit | `dynamic=False` |
| Live evaluation | `RuleEvaluator` (`evaluator.py`) | the rule's own one-shot wake-up, a source refresh, or the safety-net tick | `dynamic=True` |

### CompiledRule

| Field | Type | Default | Meaning |
|---|---|---|---|
| `trigger` | `Trigger` (discriminated on `kind`) | required | the one moment/event that fires the rule |
| `conditions` | `list[Condition]` | `[]` | all must hold before actions run |
| `actions` | `list[Action]` | `[]` | what to do, in order |
| `summary` | `str` | `""` | one-line human echo of the whole rule |
| `confidence` | `float` | `1.0` | compiler's confidence, 0..1 |
| `warnings` | `list[str]` | `[]` | compile-time caveats shown on the rule |
| `dynamic` | `bool` | `False` | rule depends on live source data, so the LLM evaluator judges it against the current world state instead of a fixed trigger |

`summary`, `confidence` and `warnings` are informational — nothing in either execution path
reads them.

The surrounding `Rule` row carries the English source plus bookkeeping: `id`, `text`,
`enabled`, `compiled` (`None` if compilation failed), `compile_error`, `source` (`user |
seed | api`), `created_at`, `updated_at`, `last_fired_at`, `last_result`, and the two
wake-up columns `next_check_at` (ISO-8601 UTC) and `next_check_reason`.

### Triggers

| Variant | `kind` | Fields |
|---|---|---|
| `ScheduleTrigger` | `"schedule"` | `cron: str` (5-field, evaluated in the engine's tz), `describe: str = ""` |
| `SignalTrigger` | `"signal"` | `signal: str` (key from `config.signals`), `edge: SignalEdge = "becomes_true"`, `comparator: Comparator = "is_true"`, `value: bool \| float \| str \| None = None`, `describe: str = ""` |
| `ManualTrigger` | `"manual"` | `describe: str = "run manually"` |

`SignalEdge` is `becomes_true | becomes_false | changes | on_value`. `Comparator` is
`is_true | is_false | == | != | > | >= | < | <=`.

Edge matching (`_trigger_matches`) derives the transition from `(prev, signal)` rather than
the bus's own edge string, so a first-ever emit (`prev is None`) counts as a transition —
that is what makes the "simulate Luke finished Atom" demo fire. The comparator is applied to
`signal.value` first via `_cmp`, which never raises (a `TypeError`/`ValueError` on the
numeric comparators returns `False`). `changes` and `on_value` are then both satisfied by
the bus's `changed` flag alone — they are indistinguishable in the engine, so `on_value`
adds only its comparator, not a distinct edge.

### Conditions

| Variant | `kind` | Fields |
|---|---|---|
| `SignalCondition` | `"signal"` | `signal: str`, `comparator: Comparator = "is_true"`, `value: bool \| float \| str \| None = None` |
| `TimeWindowCondition` | `"time_window"` | `after: str \| None` (`"HH:MM"`), `before: str \| None`, `weekdays: list[int] \| None` (0=Mon..6=Sun) |

`_eval_conditions` requires all of them; a signal the bus does not know reads as `None`,
which fails `is_true`, `==` and the numeric comparators but *satisfies* `is_false` and `!=`;
an unparseable `"HH:MM"` is ignored rather than failing the
window. Conditions gate dry runs as well as real ones, so "Run now (dry)" reports "wouldn't
run" honestly.

**No compile path is meant to emit a condition.** `compile_fallback` hardcodes `conditions=[]`, and the
LLM system prompt says `"conditions": [], // ALWAYS empty` and routes anything needing a
condition to `dynamic=True`. Nothing *strips* them on the LLM path, though, so a model that
returned one would have it validated and persisted. In practice the machinery runs only for a
hand-written or previously-persisted `compiled` blob.

### Actions

| Variant | `kind` | Fields | Executed by |
|---|---|---|---|
| `SetServiceAction` | `set_service` | `group`, `service`, `state: ServiceState` | AdGuard — `ag.set_service` |
| `SetGroupAction` | `set_group` | `group`, `state` (`blocked` = all of the group's services off) | AdGuard — `ag.set_group`, or per-service writes when holds are in play |
| `SetClientAction` | `set_client` | `client`, `state` | AdGuard — `ag.set_client` |
| `SetProtectionAction` | `set_protection` | `enabled: bool` | AdGuard — `ag.set_protection` (whole network) |
| `AddRuleAction` | `add_rule` | `rule: str` (AdGuard filtering syntax) | AdGuard — `ag.add_rule` |
| `RemoveRuleAction` | `remove_rule` | `rule: str` | AdGuard — `ag.remove_rule` |
| `SetHostServiceAction` | `set_host_service` | `service: str` (`ManagedServiceConfig.id`), `running: bool` | hosts — `ctx.hosts.set_running` |

`ServiceState` is `allowed | blocked`.

`set_host_service` is global by nature: it starts or stops the real process for everyone in
the house and ends anything mid-stream, so both prompts tell the model to use it only when
the instruction plainly means the service itself.

Targets are validated against config before dispatch, so an unknown name raises a clear
`ValueError` that `run_rule` audits (`ok=False`) rather than failing murkily inside the
AdGuard layer: `_require_group`, `_require_service`, `_require_client` (resolved against
`ag.resolve_clients()`, not config alone, because a device can join a group by AdGuard tag),
and `config.managed_service` for the host kind. A missing `ctx.adguard` or `ctx.hosts`
raises `RuntimeError`.

Manual holds are honoured per action kind. `run_rule` classifies the fire as `"signal"` when
`reason` starts with `signal:` and `"reset"` otherwise (cron, or a human pressing Run now):

* `reset` — the write *is* the reset a hold waits for, so pins are cleared **before** the
  write; clearing afterwards meant a transient AdGuard error stranded the pin and locked the
  evaluator out of the switch it could have repaired.
* `signal` — automation must not undo a parent's hand, so held switches are spared and the
  spared set is reported in the audit note.

Holds apply to `set_service`, `set_group` (partial: unheld services are written
individually, held ones counted in the note) and `set_host_service` (keyed `("_host",
service)`). `set_client`, `set_protection`, `add_rule` and `remove_rule` are not hold-aware.

### The `dynamic` flag

A rule is dynamic when it depends on live source data — someone's minutes, scores,
completion, a form — or when it names more than one moment, or needs a condition of any
kind. Both deterministic paths skip such a rule outright.

`reload_rules`, before scheduling any cron job (`engine.py:303`):

```python
            if rule.compiled.dynamic:            # dynamic rules run via the evaluator, not cron
                continue
```

`_handle_signal`, before matching any signal trigger (`engine.py:358`):

```python
            if rule.compiled.dynamic:            # dynamic rules run via the evaluator, not signals
                continue
```

The consequence: a dynamic rule's compiled `trigger` and `actions` are inert. The
deterministic compiler emits `ManualTrigger` with an empty action list for one, and the
evaluator re-reads `rule.text` from scratch at every wake-up. A dynamic rule with no LLM
backend never runs at all — `evaluate_all` returns `[{"skipped": "no LLM backend for dynamic
rules"}]`.

### Who can emit which action

| Action kind | Deterministic parser (`compile_fallback`) | LLM compiler | AI evaluator (`_parse_actions`) |
|---|---|---|---|
| `set_service` | yes | yes | yes |
| `set_group` | yes | yes | yes |
| `set_host_service` | no | yes | yes |
| `set_client` | no | yes | no |
| `set_protection` | no | yes | no |
| `add_rule` | no | yes | no |
| `remove_rule` | no | yes | no |

The deterministic parser produces **at most one** action (`actions = [action]`), chosen by
`_parse_action`: a named service wins (`set_service`, defaulting to the service's first
group with a warning and confidence 0.7 if no group is named), otherwise a recognised group
gives `set_group`. State comes from whichever of the vocabulary's
`block_words`/`allow_words` appears first. If no action is found and the text looks dynamic,
it returns a `dynamic=True` shell (manual trigger, no actions, confidence 0.6); if it is not
dynamic, it raises `ValueError`. Triggers it can emit: `SignalTrigger` (best-scoring vocab
signal, minimum score 3), `ScheduleTrigger` (a clock time and/or day token, validated with
`croniter`), else `ManualTrigger` with a warning and confidence 0.5.

`compile()` degrades to the parser on any backend error and, when it does, inserts a warning
on the rule and caps confidence at 0.5 — the parser understands one clause and a clock, so a
three-part instruction can silently compile down to a fraction of itself and still look
healthy in the UI.

The evaluator accepts only the three kinds above; anything else the model returns is dropped
silently. Each is re-validated against config: the group must exist, the service must exist
*and* belong to that group, a host service must resolve via `config.managed_service`. State
is `blocked` only for the literal string `"blocked"`, so any unrecognised value becomes
`allowed`.

### Vocabulary

`build_vocabulary(config)` derives the controlled terms from `WardenConfig`, so ids always
match the user's real setup: `groups` + `group_aliases`, `services` (each with `groups` and
aliases from `SERVICE_ALIASES`, falling back to `[id, name.lower()]`), `subjects`, `signals`
(`key`, `type`, `describe`, `subject`, `source`), `host_services`, and the
`allow_words`/`block_words` lists. `prompt_reference()` renders it for the compiler's system
prompt — including the warning that a host service affects everyone, not a group. The
fallback parser consumes the same structure (`group_aliases` in `_find_group`, `aliases` in
`_find_service`, the word lists in `_parse_state`, signals in `_parse_signal`), so both
compilers agree on the vocabulary. The evaluator does not use `Vocabulary`; it builds its
action catalog from the live AdGuard snapshot plus `config.managed_services`.

### The wake-up scheduling contract

A dynamic rule chooses its own next check as part of each evaluation, from the cadence
written into its English ("no need to check while he's at school … then hourly until 7pm").
That value is load-bearing: it is the only thing that will wake the rule, so the prompt
states plainly that nothing else will evaluate it beforehand.

**Format.** The model returns `next_check_local` as a bare local wall-clock `"YYYY-MM-DD
HH:MM"` with no offset, and `_parse_next_check` attaches the household zone here. Asking for
an offset was a real source of error — the model wrote the intended hour but stamped it
`+00:00`, so during BST every wake-up landed an hour late, inconsistently. Parsing tries
`fromisoformat` (with `Z` → `+00:00`), then `%Y-%m-%d %H:%M`, `%Y-%m-%d %H:%M:%S`,
`%Y-%m-%dT%H:%M`; a naive result gets `settings.tz` (UTC if that zone fails to load); the
result is converted to UTC. The legacy key `next_check_at` is accepted as a fallback field
name.

**Clamps.** A too-soon value would spin the evaluator and the bill; a too-far one would
strand the rule.

| Bound | Value | Result |
|---|---|---|
| `_MIN_NEXT_CHECK` | 5 minutes | `now + 5m`, reason suffixed `(clamped: asked to wake too soon)` |
| `_MAX_NEXT_CHECK` | 7 days | `now + 7d`, reason suffixed `(clamped: asked to wake >7d out)` |
| `_FALLBACK_NEXT_CHECK` | 1 hour | used when nothing parses; reason suffixed `(no usable next_check_at; defaulted to 1h)` |

The prompt states the same 5-minute / 7-day bounds to the model, so the clamp is a guard
rail rather than the normal path.

**Persistence and arming.** `evaluate_and_apply` writes the outcome and the wake-up together
via `db.touch_rule(rule.id, now, last_result[:400], next_check_at=<iso>,
next_check_reason=<why[:300]>)`, then calls `engine.arm_next_check`, swallowing any failure.
`arm_next_check` adds a one-shot `DateTrigger` job with id `eval:rule:<rule_id>`,
`replace_existing=True` (so the most recent evaluation always wins),
`misfire_grace_time=3600`, `coalesce=True`. When it fires, `_eval_one_job` calls
`evaluate_all("self-scheduled", only=rule_id, force=True)`, and that evaluation re-arms the
next one. `disarm_next_check` removes the job. The rules API clears **both** the job and
`next_check_at` when a rule's text is edited or recompiled; disabling or deleting a rule
disarms the job but leaves the stored `next_check_at` in place.

**Re-arm at startup.** `start()` calls `restore_next_checks()` after `reload_rules()`. For
every enabled, compiled, dynamic rule with a stored `next_check_at`, it parses the ISO
value, treats a naive value as household-local, and arms `max(when, now + 10s)` — anything
already overdue is nudged a few seconds out rather than dropped, so a restart cannot
silently lose a pending check.

**Safety net.** A single `eval:tick` job runs `evaluate_all("tick")` for whatever is due,
catching rules with no wake-up armed (freshly created, or a job lost to a crash between
arming and firing). Its cadence is `settings.eval_interval_min` (default 10) and
`_eval_trigger` anchors it to the wall clock, not to process start — an interval counted
from boot puts an hourly tick at 22:47, 23:47 …, which makes "when did it last check?"
unanswerable. Periods that divide the hour become `CronTrigger(minute="*/n")`; whole-hour
periods become `CronTrigger(hour=..., minute=30)` — deliberately offset from `:00`, since
rules self-schedule and sources poll on the hour and three paths were landing on the same
rule in the same minute; anything else falls back to an `IntervalTrigger`.

### `is_due` vs `wakeable_early`

Both are pure functions of `rule.next_check_at` (missing or unparseable ⇒ `True`; naive ⇒
read as UTC).

* `is_due(rule, now)` — `now >= next_check_at`. While that timestamp sits in the future the
  rule is deliberately asleep and nothing, not the tick and not a source refresh, wakes it;
  that is what makes "no need to check while he's at school" actually hold instead of being
  overridden by the hourly Atom poll.
* `wakeable_early(rule, now)` — `(next_check_at - now) <= EARLY_WAKE_WINDOW` (2 hours). New
  data may pull a rule forward only if it was about to look anyway. The early wake exists
  for the rule *waiting* on the data (Luke finishing at 16:52, collected at 17:00, acted on
  at 18:00 was the bug), which checks hourly and so is minutes away; a rule that has said
  "on track, don't check again until tomorrow at 4pm" has answered its question and would
  only pay ~20k tokens to be told the same thing.

`evaluate_all` admits a rule when `force`, or `is_due(r)`, or `on_data_change and
wakeable_early(r)`. `on_data_change` is passed by the adapter registry as the source's
`changed` flag after a successful collection, and the registry clears that flag only after a
sweep that actually happened — `evaluate_all` has refusal paths that return rather than
raise. `force=True` comes from exactly one place — the rule's own wake-up job
(`engine._eval_one_job`). The "Evaluate now" button does **not** go through `evaluate_all` at
all: `evaluate_one` applies the stale-data guard and evaluates the single rule directly.

Before any decision, `_await_collection` awaits `registry.ensure_fresh()`: a rule's wake-up
and the source poll it depends on are both hour-anchored and fire together, and the
evaluator reads *stored* state, so without the wait a rule acts a full cadence late. Waiting
here also covers a slow scrape, a restart mid-cycle, and an overdue poll.

### The duplicate-evaluation guard

Three schedules legitimately land on the same minute — the rule's own wake-up, the
safety-net tick, and a source refresh — and each read `is_due` before any of them had
written an answer, so one 08:00 produced three concurrent LLM calls that disagreed in the
log. `_evaluate_guarded` closes that:

1. Take the per-rule `asyncio.Lock` from `self._locks` (one in-flight evaluation per rule).
2. Re-read the row from the DB (`fresh`), so the losers see the winner's write.
3. If `fresh.last_fired_at` is less than `_MIN_GAP_SECONDS` (60) ago, log and return `None`.
   This check runs even under `force`.
4. Re-check dueness against the fresh row under the same admission rule the caller was let
   in with (`force`, or `is_due`, or `on_data_change and wakeable_early`), else return
   `None`.

Accepted narrow limitation, noted in the registry: a rule that evaluated on pre-change data
less than 60s before a sweep is deduped here and catches up on its own next check.

### Refusing to act on stale data

Dynamic rules flip real network switches, so they must never run on offline sample data.
This is not hypothetical — an expired Atom session made the adapter serve fixtures ("5
islands all-time" against a real 403), the run was logged as a success, and the rule kept
the kids blocked after Luke had finished his work.

`stale_sources()` walks every **enabled** configured source and reports it as untrustworthy
when:

| Condition | Reported `why` |
|---|---|
| no `state:<key>` in the KV store | `no data collected yet` |
| stored state does not parse | `unreadable stored state` |
| `data_quality.live is not True` | `data_quality.note`, else `not live data` (plus `collected_at`) |

In `evaluate_all`, after `_await_collection` and before any LLM call: if anything is stale,
every candidate rule gets `touch_rule(... "skipped (<tag>): source data is not live —
<note>"[:400] ...)` **preserving its existing `next_check_at`/`next_check_reason`** (so the
pending wake-up is not lost), one `ok=False` audit line is written against
`evaluator/rule_eval/all-dynamic`, a warning is logged, and the call returns `[{"skipped":
"source data is not live", "sources": [...]}]`. Leaving the switches where they are is the
lesser harm. `evaluate_one` applies the identical guard before its snapshot, because relying
on the model to notice a warning in the payload is not a safety mechanism.

`build_world` additionally injects a `DATA_WARNING` block naming the stale sources — belt
and braces beside the hard guard. Since both gated entry points return before the model is
called, that block reaches the model only through the dry-run path in the rules API, which
calls `build_world` + `evaluate_rule` directly and applies nothing.


---

## Writing a source adapter

A source is anything Warden can log into and read. An adapter does two mechanical jobs —
fetch + normalise to `Item[]`, derive `Signal[]` — plus assemble a free-form `state` dict
for the LLM rule evaluator. It decides nothing: relevance, summarisation and what to flip
belong to the hub.

Everything lives in `warden/adapters/<name>/`. The hub side is `SourceRegistry`
(`warden/adapters/registry.py`); the contract is `warden/adapters/base.py`.

### The ABC

```python
class SourceAdapter(abc.ABC):
    manifest: AdapterManifest                       # class attribute; subclasses set it

    @abc.abstractmethod
    async def authenticate(self, ctx: SourceContext) -> dict[str, Any]: ...
    @abc.abstractmethod
    async def collect(self, ctx: SourceContext, session: dict[str, Any]) -> CollectResult: ...
    @abc.abstractmethod
    async def healthcheck(self, ctx: SourceContext) -> HealthResult: ...
```

| Method | Contract |
|---|---|
| `authenticate(ctx)` | Establish a session; return a persistable/refreshable blob (may be `{}`). The three shipped adapters return `{"mode": "live" \| "fixtures", ...}` and put a human-readable `"warning"` on the fixtures branch; `collect` reads it back. |
| `collect(ctx, session)` | Fetch since `ctx.since_cursor`, normalise to `Item[]`, derive `Signal[]`, build `state`. |
| `healthcheck(ctx)` | Cheap liveness/structure check. **No caller anywhere in the repo** — all three adapters implement it, nothing invokes it. Implement it (the ABC requires it) but nothing depends on the result. |

`manifest` is a plain annotation, not an abstract property: omitting it fails later, at `GET
/api/sources`, not at discovery. Keep the adapter stateless — the hub owns cursors and
state.

### SourceContext

Assembled fresh per run by `SourceRegistry._build_context`.

| Field | Type | Source of the value |
|---|---|---|
| `source_key` | `str` | the `sources:` map key. Set, but no shipped adapter reads it. |
| `subject` | `Optional[str]` | `sources.<key>.subject` — the child the run is about |
| `secrets` | `dict[str, str]` | `resolve_secrets()`: logical name → `os.getenv(env_var, "")`. Values may be empty strings. |
| `options` | `dict[str, Any]` | `sources.<key>.options`, **plus** the hub-resolved weekly target (see below) |
| `since_cursor` | `Optional[str]` | `kv["cursor:<key>"]`, whatever the last run returned as `next_cursor` |
| `fixtures_dir` | `Optional[str]` | `<adapter package>/fixtures`, recorded at discovery |
| `live` | `bool` | the `live` argument if given, else `True` |

`has_secrets` (property) is `bool(secrets) and all(secrets.values())` — every declared
secret present and non-empty. `fixtures_path()` returns `Path(fixtures_dir)` or `None`.

`live` defaults to `True` because Atom authenticates from a captured Google session rather
than secrets, so `has_secrets` is a poor gate for "can this run for real". Fixtures
therefore only happen when a caller explicitly asks for `live=False`.

### CollectResult

| Field | Default | Meaning |
|---|---|---|
| `ok` | `True` | run succeeded. Recorded on the `SourceRun` and gates the rule sweep. |
| `live` | `False` | data came from the real source, not fixtures |
| `items` | `[]` | normalised `Item[]`, deduped centrally on `external_id` |
| `signals` | `[]` | derived `Signal[]` |
| `state` | `{}` | the "state of play" the dynamic evaluator reasons over |
| `next_cursor` | `None` | opaque; stored only when not `None` |
| `detail` | `""` | one-line summary for the run log and the Sources card; the hub synthesises one if empty |

The hub persists items, signals and state **without checking `ok`**, so a failed result must
carry nothing: return a bare `CollectResult(ok=False, live=False, detail=...)`. An empty
`state` is what leaves `kv["state:<key>"]` untouched, which is what "kept the previous data"
actually means.

#### `state` — the LLM's view

`state` goes into `kv["state:<key>"]` verbatim and reappears as `world["sources"][<key>]` in
the evaluator prompt (`RuleEvaluator.build_world`). It is free-form; put structured facts
there rather than only in prose items. Atom's carries `today`, `week_to_date` (with a nested
`target` block), `last_active_day`, `recent_days_minutes`, `assignments_parent_set`,
`attainment`, `recent_mock_tests`.

Two hub conventions attach to it:

- **`state["data_quality"]` is mandatory in practice.** Shape: `{"live": bool,
  "collected_at": <iso>, "note": <str>}`. `RuleEvaluator.stale_sources()` treats any enabled
  source whose stored `data_quality.live` is not exactly `True` as untrustworthy and **skips
  every dynamic rule in the system**, not just rules touching that source. A new adapter
  that stores state without it silently freezes the rules engine. `list_sources()` reads the
  same two fields for the red "Not live data" banner.
- **Change detection strips clocks.** `Db.record_state_snapshot` hashes the state with
  `now`, `collected_at`, `fetched_at` and `generated_at` removed at any depth, and archives
  a snapshot only when the hash moves. The return value is load-bearing: it sets the
  registry's `_changed_since_eval[key]` flag, which is what wakes a rule that was about to
  look. Put a timestamp anywhere else in `state` and every poll looks like a change, which
  is the same as no changes ever.

The hub also injects `state["documents"]` before storing, if any items carried readable
attachments (below).

#### Items and signals

`Item` (`warden/models.py`): `source_id`, `account_id="default"`, `subject_ids`,
`external_id`, `kind`, `title`, `body_text`, `occurred_at`, `due_at`, `audience_tags`,
`url`, `attachments`, `raw`, `fetched_at`. `external_id` must be stable across runs —
`save_items` is `INSERT OR IGNORE` on it, and that is the entire dedupe mechanism. `raw` is
kept whole for the LLM. `audience_tags` is read by the reminders page and the UI;
`subject_ids` is stored but nothing in the hub reads it back.

`Signal`: `key`, `value`, `type` (`bool|number|string`), `source`, `subject`, `at`, `meta`.
Signals go onto `SignalBus`, which computes the edge
(`init|same|becomes_true|becomes_false|changes`) that rule triggers match on, so the value
type matters — a bool signal must be a real bool. A signal also needs a `signals:` entry in
`config/warden.yaml` to get a `describe` string in the evaluator prompt and a declared type
for `POST /api/signals/emit`.

#### Attachments the hub can read

If an item's `raw["_files"]` holds `[{"key", "path", "name", "ext", ...}]` entries pointing
at downloaded files, `DocumentReader.process_items` reads any PDF among them and the digests
land in `state["documents"]`. Adapters only fetch files; interpreting them is the hub's job,
and a failure there never fails the run — an unread newsletter is a gap, not an outage.

### HealthResult

`ok: bool = True`, `detail: str = ""`. Nothing calls `healthcheck()`.

### AdapterManifest and manifest.json

```python
class AdapterManifest(BaseModel):
    id: str
    display_name: str
    version: str = "1.0.0"
    auth_type: str = "form_login_session"    # form_login_session | api_key | none
    capabilities: list[str] = []
    secrets_required: list[str] = []
    emits_signals: list[str] = []            # signal keys this adapter can emit
```

Every shipped adapter parses its own file at class-definition time:

```python
class Adapter(SourceAdapter):
    manifest: AdapterManifest = AdapterManifest.model_validate_json(
        _MANIFEST_PATH.read_text(encoding="utf-8"))
```

So a malformed `manifest.json` fails discovery, not collection. `id` and `display_name` are
required; everything else defaults. Extra keys are ignored (the historical spec's
`schedule_hint` has no field). `auth_type` is a free string — Atom ships
`google_sso_session`, outside the documented three.

The manifest is advertisement only. `list_sources()` returns it under `manifest`
(substituting `<subject>` in `emits_signals` for display), and nothing — not the API, not
the web UI, not the rules engine — reads `capabilities`, `secrets_required` or
`emits_signals`. Nothing validates that what the adapter emits matches what it declares;
Weduc emits `weduc.<subject>.lunch_booked_tomorrow` on live runs and does not declare it.

`manifest.id` is not the identity the hub uses: `sources.<key>` is the identity and
`sources.<key>.adapter` is the package name.

### Fixtures and `SourceContext.live`

Every adapter MUST run in fixture mode: with no secrets and no browser it loads saved
captures from `fixtures/` and still returns items, signals and state, so the whole app demos
offline. The registry records `<adapter package>/fixtures` at discovery and hands it over as
`ctx.fixtures_dir`; ParentPay and Weduc read it through `ctx.fixtures_path()`, Atom uses its
own `_HERE / "fixtures"`.

Fixtures are dated samples, so keep them from going stale: Atom re-stamps any island flagged
`today` with a real now-ish time; ParentPay stores days as offsets from today.

A fixtures run returns `ok=True, live=False` with a `data_quality.note` that says so in
capitals — `"SAMPLE DATA — offline fixtures, not Luke's real progress"`. That combination is
exactly what makes the evaluator refuse to act.

### The hard rule: a failed live run returns `ok=False` and never substitutes fixtures

From `CollectResult` in `base.py`:

> Did this data actually come from the live source? Fixtures are for demoing with no
> credentials — they must never be mistaken for real data by the rules engine, which flips
> real network switches. An adapter asked for a LIVE run that fails must report `ok=False`
> rather than quietly substituting the sample.

From the Atom adapter's `collect`, on the live-fetch exception path:

> Do NOT fall back to fixtures here. This used to look like a successful run carrying sample
> data ("5 islands all-time" against a real 403), and a dynamic rule acted on it — keeping
> streaming blocked after Luke had actually done his work. A failed live fetch is a failure,
> and the last known-good state stays untouched.

And `RuleEvaluator.stale_sources`, the backstop:

> Dynamic rules flip real network switches, so they must never run on the offline sample.
> This is not hypothetical: an expired Atom session made the adapter serve fixtures ("5
> islands all-time" against a real 403), the run was logged as a success, and the rule kept
> the kids blocked after Luke had finished his work.

The three-branch shape every adapter's `collect` follows:

```python
if session.get("mode") == "live":
    try:    return await self._collect_live(ctx, subject)
    except Exception as e:
        return CollectResult(ok=False, live=False, detail=f"live fetch failed (…) — kept the previous data rather than substituting fixtures")
if ctx.live:               # live was asked for, authenticate() could not deliver
    return CollectResult(ok=False, live=False,
                         detail=session.get("warning") or "…refusing to pass fixtures off as real")
return self._collect_fixtures(ctx, subject, warning=session.get("warning", ""))
```

ParentPay words its own failure `"…rather than reporting no bookings"` — for that source the
dangerous lie is absence, not stale numbers.

### Discovery and fault isolation

`SourceRegistry.start()` walks `config.sources` and, per entry, imports
`warden.adapters.<source.adapter>.adapter` and instantiates `getattr(mod, "Adapter")()`. So
the required layout is a package with an `adapter.py` exposing a class literally named
`Adapter`; re-exporting it from `__init__.py` is optional (ParentPay does not).

Each discovery is wrapped: a broken adapter is recorded in `_errors[key]` and logged, never
raised — "a broken adapter must not sink the others". The error surfaces as `error` on that
source's `list_sources()` row, and the source is simply not scheduled. `_require_adapter`
retries discovery lazily on the next run, so a fixed adapter recovers without a restart
(though `_errors` is only ever written during `start()`, so a stale error string can outlive
the fault).

Run-time faults are isolated too: `_collect_and_store` catches every exception, writes a
failed `SourceRun` and returns it, so a scheduler tick can never crash the event loop.

### Scheduling

Sources with `enabled`, a `schedule` and a discovered adapter get an APScheduler job:

```python
CronTrigger.from_crontab(source.schedule, timezone=settings.tz)
add_job(self.run_source, trigger, args=[key], kwargs={"scheduled": True},
        id=f"source:{key}", replace_existing=True,
        coalesce=True, max_instances=1, misfire_grace_time=300)
```

Standard five-field crontab in the configured timezone. An invalid expression is logged and
that source goes unscheduled — it does not fail discovery. **Nothing runs on boot**;
scheduling only arms future ticks. A run is always a cron tick, an operator/API "run now",
or `ensure_fresh` deciding a poll is overdue.

`scheduled=True` marks the cron tick and makes it *skip its own scrape* when the tick is
already covered: it joins an in-flight collection, or takes the last run if that run
succeeded, then goes straight to the evaluation sweep. Twin scrapes at :05 and :08 every
hour bought nothing and doubled the load on the session. Only a **successful** last run
counts as covering the tick — the tick is the retry cadence, so a failure must not freeze
the hour. Manual "Run now" keeps `scheduled=False`: an operator asking for a run gets one.

### One collection in flight per source

`_inflight: dict[str, asyncio.Task]`, keyed by source. `collect_now` → `_collection_task`
returns the existing task if one is running, so a cron tick, a waiting rule and a "run now"
that land together join the same run instead of racing three scrapes. `collect_now` wraps
the await in `asyncio.shield`, so a caller that gives up on its own timeout cannot abort a
scrape everyone else is waiting on. The done-callback removes the entry only if it is still
the same task.

Joining ignores the joiner's `live` argument — pressing "Fixtures" while a live collection
is running returns the live run.

`collect_now` collects and persists only; it must never call back into the evaluator, or
`evaluate → wait for data → evaluate` becomes a cycle. Rule evaluation happens one level up,
in `run_source`.

### `ensure_fresh()` — the collect-before-evaluate barrier

```python
async def ensure_fresh(self, timeout: Optional[float] = None) -> list[str]
```

Called by `RuleEvaluator._await_collection`, immediately before the stale check and the
sweep — data first, decisions second. It blocks until every enabled source's stored data is
as current as its schedule says it should be:

- a collection already in flight is awaited (the common case — the hourly poll and a rule's
  own wake-up are both anchored to the hour);
- a source whose scheduled poll has come round with no run to show for it is collected now,
  and awaited.

Sources are done **one at a time**: they are scrapes, and two browser logins at once is a
worse failure than a few seconds of extra wait. Each is bounded by `timeout`, default
`settings.collect_wait_sec` (`WARDEN_COLLECT_WAIT_SEC`, 180s); on expiry the evaluator
judges the data already stored and leaves the collection running. Returns the keys waited
on.

`_collection_due` compares the last **attempted** run against the previous cron fire, plus a
5-second `_DUE_LEAD` so a rule waking on the hour cannot win the race against the source job
scheduled for the same instant. Attempted, not succeeded, so a failing source (expired
session, site down) is not hammered by every rule evaluation between ticks. Known and
accepted: during the DST fall-back's repeated wall-clock hour the previous fire computes an
hour early, so hourly data can be judged fresh at two real hours old.

Ordering, stated at the top of `registry.py`: collection completes before rules are
evaluated, always. A rule evaluated mid-collection is judging the previous cycle's data,
which shows up as the rule acting a whole cadence late.

### The run pipeline

`run_source(key, live=None, scheduled=False)`:

1. if `scheduled` and not due — join in-flight, or reuse the last successful run; else
2. `collect_now(key, live)` → `_collect_and_store`:
   `start_run` → `authenticate` → `collect` → `save_items` (dedupe on `external_id`) → per
   signal `bus.emit` + `emit_signal_update` → store `next_cursor` if not `None` → read
   documents → merge into `state` → store `state` → `record_state_snapshot` → `finish_run` →
   `audit` → `after_change("source")`
3. if `run.ok` and an evaluator exists — `evaluate_all(f"source:{key}",
   on_data_change=changed)`.

`changed` is the archived-snapshot flag. It is peeked and cleared only after a sweep that
really happened, because `evaluate_all` has refusal paths that return rather than raise
(stale data, no LLM backend) and a `CancelledError` that `except Exception` never sees. It
is not a blanket force: it wakes a rule that was about to look, and leaves a rule that has
deliberately slept until tomorrow asleep.

Documents are read **before** the state is stored, so their digests land in the same
state-of-play the rules reason over rather than a cycle behind.

### The per-week target round-trip

The hub owns the weekly workload target so adapters stay stateless.
`SourceRegistry.get_target(source, week=None)` resolves, in precedence order:

1. **override** — `kv["target:<source>:<subject>:<week-monday>"]`, set by `PUT
   /api/sources/{key}/target` (`{islands, week_start?, note?}`);
2. **atom-published** — `published_target()`, read back out of the stored state at
   `state.week_to_date.target.published_by_atom`, and only if
   `state.week_to_date.week_starts_monday` matches the week asked for, since a snapshot from
   another week describes another week's plan;
3. **config-default** — `sources.<key>.options.weekly_island_target`, else `origin:
   "unset"`.

The winner is injected back into the adapter's context by `_options_with_targets` as
`options["weekly_island_target"]` + `options["weekly_island_target_origin"]`. The adapter
re-reads the source's own plan live and prefers it, so what travels in matters in two cases
only: an override, which must beat the plan, and a run that could not fetch the plan, which
then keeps the last good number instead of falling back years. Atom's `_resolve_target`
implements exactly that precedence.

`set_target` / `clear_target` (`PUT` / `DELETE /api/sources/{key}/target`) both call
`refresh_after_target_change(key)`, which sets `_changed_since_eval[key] = True` and fires a
background re-collection — nothing in the data moved but the bar did, and the daily share is
derived from the weekly number, so the new target must bite now rather than at the next
hourly poll. The task is held in `_background` so the loop cannot garbage-collect it
mid-run.

`GET /api/sources/{key}/target` adds `today`, read back out of the same stored state
(`week_to_date.target.todays_requirement`) rather than recomputed, so the API and the
`daily_complete` signal cannot drift apart. The UI treats a 404/absent target as "this
source has no target concept" — the round-trip is opt-in, and a new adapter gets it only by
publishing `week_to_date.target.published_by_atom` (with a `total`) in its state under those
exact key names.

### Adding a source, end to end

1. **Recon first.** Capture the session by hand and record the XHRs the site's own app
   makes, so the adapter can hit JSON rather than scrape a DOM.
   `warden/adapters/weduc/login.py --recon` and `warden/adapters/amazon/login.py` are the
   pattern; `warden/adapters/amazon/` is a recon tool and nothing else — no adapter, no
   manifest, no config entry.
2. **`warden/adapters/<name>/__init__.py`** — may be empty.
3. **`warden/adapters/<name>/manifest.json`** — at minimum `id` and `display_name`; parsed
   at import, so a bad one fails discovery.
4. **`warden/adapters/<name>/login.py`** if the site needs a browser once. All three shipped
   sources capture a session once (`python -m warden.adapters.<name>.login`) into
   `data/<name>_state.json` and replay it headlessly with httpx thereafter, so the container
   needs no browser at collect time. If the site logs in with a username and password, make
   the adapter re-capture unattended (Weduc) rather than asking a human (Atom's Google SSO).
5. **`warden/adapters/<name>/fixtures/`** — a captured sample, dates relative or re-stamped
   so it never goes stale.
6. **`warden/adapters/<name>/adapter.py`** — `class Adapter(SourceAdapter)` with the
   manifest class attribute and the three methods. Emit `Item[]` with stable `external_id`s,
   `Signal[]`, and a `state` dict carrying `data_quality`.
7. **`config/warden.yaml`** — a `sources:` block (`adapter`, `display_name`, `enabled`,
   `schedule`, `subject`, `secrets:` mapping logical name → env var, `options:`), and a
   `signals:` entry per signal key with its `type` and a `describe` the evaluator will read.
8. **`.env`** — the env vars named in `secrets:`.
9. Restart. Check the Sources card: no red discovery error, "Run live" gives `ok` with a
   `live` detail line and no "Not live data" banner.
10. **Reference the signal in a plain-English rule.** No core changes.

Docker: `./data` is bind-mounted read-write so a freshly captured session file is a host
copy, and so a sliding cookie the adapter refreshes can be written back.

The one hub component that is *not* source-agnostic is the reminders page
(`warden/reminders.py`), which reads `state:weduc` and `state:parentpay` by name — a
new source appears in the rules engine and the Sources view automatically, but not there.


---

## AdGuard

Three files, one class the rest of Warden talks to:

| File | Role |
| --- | --- |
| `warden/adguard/client.py` | `AdGuardClient` — thin async httpx wrapper over the AdGuard Home REST API |
| `warden/adguard/fake.py` | `FakeAdGuard` — in-memory stand-in with the identical low-level surface |
| `warden/adguard/service.py` | `AdGuardService` — the public API; `Backend` protocol; `build_adguard()` |

`AdGuardService` is **pure**: it changes AdGuard state and returns view models, but never
audits and never pushes UI events. Callers (API routes, rules engine) wrap each mutation
with `ctx.audit(...)` then `ctx.after_change(...)`.

### REST endpoints

Verified live on **AdGuard Home v0.107.78**. HTTP Basic auth, credentials from `Settings`;
the auth tuple is keyed on the username alone, so an empty `ADGUARD_USER` means no auth
header at all regardless of `ADGUARD_PASS`. One lazily-opened `httpx.AsyncClient` per
`AdGuardClient`, `Timeout(10.0, connect=5.0)` — the short connect timeout keeps an
unreachable box from stalling boot or probe.

| Endpoint | Used by | Notes |
| --- | --- | --- |
| `GET /control/status` | `get_status` | `{version, protection_enabled, running, …}`; also the liveness probe |
| `GET /control/clients` | `get_clients`, `get_client` | returns the `clients` array only — `auto_clients` are DHCP guesses and are ignored |
| `POST /control/clients/update` | `set_client_services` | body `{"name": <existing>, "data": <full client>}`; **overwrites the whole client** |
| `POST /control/clients/add` | `add_client` | body is a full client object |
| `GET /control/filtering/status` | `get_user_rules` | returns `(user_rules, enabled)` |
| `POST /control/filtering/set_rules` | `set_user_rules` | body `{"rules": [...]}`; **replaces the whole list** |
| `POST /control/protection` | `set_protection` | body `{"enabled": bool}`; whole-network |
| `GET /control/blocked_services/all` | `get_blocked_services_catalog` | catalogue of the 136 blockable services |

**GET-merge-PUT** applies to the two endpoints that overwrite whole objects — despite the
name, both are POSTs:

- `set_client_services(name, services)` — GET the client, copy it, replace
  `blocked_services`, force `use_global_blocked_services = False` (per-service blocking only
  takes effect when the client is not deferring to the global list, so it is asserted on
  every write), POST the rest back intact. Raises `ValueError` if no client of that name
  exists.
- `add_user_rule` / `remove_user_rule` — GET `user_rules`, add or filter one entry, POST the
  entire list. Both are no-ops when the rule is already present/absent.

`set_protection` and `add_client` need no merge and write directly.

`Backend` (the protocol `service.py` codes against) covers `get_status`, `get_clients`,
`get_client`, `get_user_rules`, `set_client_services`, `add_client`, `set_protection`,
`add_user_rule`, `remove_user_rule`, `close`. `set_user_rules` and
`get_blocked_services_catalog` exist on both concrete backends but are outside the protocol,
so nothing reaches them through the service. `add_client` is in the protocol but
`AdGuardService` never calls it — provisioning happens out of band.

### Canonical service-state model

A service is **blocked** for a group when its id appears in `blocked_services` of the
group's clients, **allowed** otherwise. (`ServiceState.allowed` / `ServiceState.blocked`; in
UI terms ON = allowed, OFF = blocked.)

- `set_service` adds/removes one service id across the group's clients.
- `set_group` adds/removes **all** managed service ids — `config.services_for_group(group)`,
  i.e. every `ServiceConfig` listing that group in its `groups`.

In a `snapshot()`, for each group and each managed service, over `existing` = the group's
clients that are actually present in AdGuard:

```python
blocked_on = [c.name for c in existing if s.id in c.blocked_services]
blocked    = len(blocked_on) == len(existing)
ServiceToggle(
    state      = ServiceState.blocked if blocked else ServiceState.allowed,
    mixed      = bool(blocked_on) and not blocked,
    blocked_on = blocked_on if not blocked else [],
)
```

- **`state` stays binary and keeps its historical meaning** — blocked iff *every* existing
  client blocks. A group summary can therefore lie: one device under a timed unblock makes
  the whole card read `allowed` while the living-room TV is still blocked.
- **`mixed` carries the split** and `blocked_on` names the devices still blocking, so the UI
  shows the truth rather than the average. `blocked_on` is deliberately emptied when
  `blocked` is true, where it would be redundant.
- The reason the split rides alongside instead of becoming a third state: `state` is what
  the evaluator sees. `RuleEvaluator.build_world` sends the model `adguard_switches =
  [{group, service, state}]` and nothing else, and its idempotence rule ("do not return an
  action whose target is already in the wanted state") is written against those two values.
  `mixed`/`blocked_on` are consumed only by the dashboard (`web/static/js/app.js`).
- **No clients in the group** — `existing` empty: `blocked = s.id in group.default_blocked`,
  i.e. fall back to configured intent; `blocked_on` stays `[]` and `mixed` stays `False`.
  Clients that are configured but absent from AdGuard are carried in `GroupState.clients`
  with `exists_in_adguard=False` and excluded from the vote.
- `GroupState.all_blocked` is `bool(toggles) and all(t.state is ServiceState.blocked ...)` —
  a group with no managed services is never `all_blocked`.

Example: kids group, `Asta iPad` blocks `youtube`, a second kids device does not →
`state=allowed`, `mixed=true`, `blocked_on=["Asta iPad"]`.

### Group membership

`_resolve_clients(raw_clients)` produces the managed set in two passes:

1. **Explicit** — every `ClientConfig` in `config.clients`, matched to AdGuard by exact
   `name`. These are claimed first and always win: a device named in config keeps its
   configured group, whatever tags it carries.
2. **Tag-discovered** — each remaining AdGuard client with a non-empty `tags` list joins the
   first group whose `match_tags` it carries **in full** (`set(g.match_tags) <= tags`).
   `[user_child, device_tablet]` means "a tablet belonging to a child", not "any tablet or
   any child's device" — a device-type tag alone never implies a group.

Groups are tried most-specific-first (`sorted(key=-len(match_tags))`; the sort is stable so
config order breaks ties), so a narrow group never loses a device to a broader one. Groups
with no `match_tags` are not candidates. A device joins **at most one** group, so it can
never receive conflicting writes.

Tag-discovered devices are fully managed: they are appended as real
`ClientConfig(discovered=True)` entries with `ids` copied from AdGuard, and every read and
write path uses the resolved list. `RuleEngine._require_client` validates against
`resolve_clients()` rather than config for exactly this reason. `discovered` propagates to
`ClientState.discovered` and `DeviceInfo.discovered` so the UI can show how a device got in.
`ClientConfig.subject` is only ever set on explicit entries — a discovered device has no
subject.

`FakeAdGuard` differs: it seeds one client per `config.clients` at construction and invents
nothing, so offline there is nothing to discover. Each seeded client gets `tags =
[group.tag]` when the group has one, but that name is already claimed by its explicit entry,
so the tag branch is dead unless something injects a tagged client through `add_client` at
runtime.

### Mode selection

`connect()` picks the backend once and is idempotent (`_connected`). Before it runs — and
after any failure — `_backend` is the fake and `mode == "fake"`, so every method is safe to
call before connect.

| `ADGUARD_MODE` | Condition | Backend |
| --- | --- | --- |
| `live` | none; no probe runs | live — an `AdGuardClient` is constructed on the spot |
| `auto` | `adguard_user` **and** `adguard_pass` both non-empty **and** `GET /control/status` returns without raising | live — the proven client is retained and reused |
| `auto` | either credential empty (probe short-circuits, no request sent) | fake |
| `auto` | probe raises anything at all | fake — the probe client is closed and discarded |
| `fake`, or any other value | — | fake |

Note the asymmetry: explicit `live` performs no credential check and no reachability check.
`close()` closes the live client if one exists and clears `_connected`, so a later
`connect()` re-runs selection.

`status()` catches every exception from `get_status()` and returns
`AdGuardStatus(reachable=False, mode, url, detail=str(e))` — an unreachable box is reported,
not crashed on. `FakeAdGuard.get_status()` returns `{"version": "fake-adguard",
"protection_enabled": …, "running": True}`.

### `AdGuardService` public surface

Attributes: `settings`, `config`, `url` (`settings.adguard_url`), `mode` (`"live" |
"fake"`), and `active_overrides: Callable[[], set[str]]` — the names currently exempt from
group writes. `main.py` points it at the DB (`lambda:
db.active_override_names(utcnow().isoformat())`); it is a callable rather than a set so the
service stays free of storage concerns and every write sees the live answer instead of a
stale copy.

| Signature | Honours overrides | Notes |
| --- | --- | --- |
| `build_adguard(settings: Settings, config: WardenConfig) -> AdGuardService` | — | module-level constructor |
| `async connect() -> None` | — | idempotent backend selection |
| `async close() -> None` | — | closes the live client only |
| `async status() -> AdGuardStatus` | — | never raises |
| `async list_clients() -> list[ClientState]` | — | managed devices only (config-named + tag-matched), flagged `exists_in_adguard` |
| `async resolve_clients() -> list[ClientConfig]` | — | membership as it stands right now; used for validation |
| `async list_devices(overrides: Optional[dict[str, Any]] = None) -> list[DeviceInfo]` | reports only | **every** device AdGuard knows, managed or not, so it is obvious why nothing is blocked on an unmanaged one; annotates each with its override from the passed map; sorted managed-first, then group, then name |
| `async snapshot() -> StateSnapshot` | reports only | status + one `GroupState` per configured group; the `mixed` split is how an exemption becomes visible |
| `async set_service(group: str, service: str, state: ServiceState) -> None` | **yes** | validates group and service, then `_apply` |
| `async set_group(group: str, state: ServiceState) -> None` | **yes** | all of `services_for_group` — the bedtime switch |
| `async set_client(client: str, state: ServiceState) -> None` | **no** | one named device; the path rules take |
| `async set_device_services(name: str, state: ServiceState) -> None` | **no, deliberately** | the override page's direct lever |
| `async set_protection(enabled: bool) -> None` | n/a | whole-network; overrides have no network dimension |
| `async list_rules() -> list[str]` | — | AdGuard user rules (the `enabled` flag is discarded) |
| `async add_rule(rule: str) -> None` | — | |
| `async remove_rule(rule: str) -> None` | — | |

**Where the exemption is enforced.** Only `_apply(group, svc_ids, state)`, the shared body
of `set_service` and `set_group`, calls `self.active_overrides()`. Per group member it skips
devices absent from AdGuard, skips exempt names (a manual unblock outranks the group), and
writes only when the computed set actually differs — no-op writes never hit the box.

The two per-device levers bypass the exemption on purpose, because a per-device instruction
is the exemption's own subject:

- `set_device_services` is used precisely when an exemption is being created or lifted
  (`routes_devices.unblock_device`, `cancel_override`, and `OverrideKeeper.sweep`
  re-blocking on expiry) and so must not be blocked by the override it is establishing or
  clearing. It raises `ValueError` if the name is not present in AdGuard, then if the device
  is in no Warden group.
- `set_client` is the rules-engine path and refuses unknown names: `ValueError` if the name
  resolves to no group, then if `get_client` finds nothing in AdGuard. `POST
  /api/actions/client` records or withdraws the `DeviceOverride` itself around the call, so
  the group writer honours it afterwards.

Both compute `blocked | svc_ids` or `blocked - svc_ids` over the device's group's managed
services and write only on a real difference.

### Read from config, never written

Warden's writes touch exactly two fields of a client object — `blocked_services` and
`use_global_blocked_services` — plus the global protection flag and the user-rules list.
Everything else in the client object survives untouched, which is the whole point of the
GET-merge-PUT.

- **`groups.<g>.baseline.{safe_search, parental, safebrowsing}`** — loaded into the
  `Baseline` model by `load_config`, described in `warden.yaml` as "always-on 'fully
  controlled' settings (never toggled at runtime)". **The live `AdGuardClient` never sends
  them.** The only consumer in the package is `FakeAdGuard._make_client`, which maps them
  onto `parental_enabled` / `safebrowsing_enabled` / `safesearch_enabled` of its in-memory
  client objects so the offline picture resembles the real box. On the live box those flags
  (and the `safe_search` sub-object) are set out of band by the separate provisioning tool
  `iac/deploy.py`, from its own `iac/screentime.yaml`; Warden then preserves whatever it
  finds there on every write.
- **`groups.<g>.default_blocked`** — read at snapshot time as the fallback for a group with
  no provisioned client, and by `FakeAdGuard` when seeding. Never pushed to the live box by
  the service.
- **`groups.<g>.tag`** — surfaced as `GroupState.tag` and used by `FakeAdGuard` to fabricate
  seeded tags. The live client never writes `tags`; tagging is done in AdGuard, and that
  alone is what pulls a device into a group.
- **`clients[].ids` / `clients[].subject`** — carried into `ClientState` and `DeviceInfo`
  for display and for linking a device to a person; neither is ever sent to AdGuard.
- **`adguard.url` in `warden.yaml`** — surfaced by `GET /api/config` for display only. The
  connection URL is `Settings.adguard_url` (`ADGUARD_URL`), which is what both
  `AdGuardClient` and `AdGuardService.url` use.


---

## Host control

`warden/hosts.py` gives Warden one non-DNS lever: run a start/stop command on a machine it
can SSH into. Everything else in Warden is AdGuard filtering.

### Why it exists

From the module docstring:

> Why this exists: every other switch in Warden is DNS filtering, which stops a device
> *looking up a name*. It cannot stop a device that already knows an address — so a media
> server on your own LAN stays reachable no matter what the resolver says. Verified on this
> network: Plex clients find the LAN server by GDM broadcast and connect straight to its IP,
> never asking DNS at all.
>
> Stopping the process is the only absolute answer, and it is deliberately modelled apart
> from the per-group toggles because it behaves differently:
>
>   * It is **global**. There is no "off for the kids" — the service is up or it is down,
> for everyone in the house, and anything mid-stream stops.
>   * It is **stateful on someone else's box**. If Warden is down when a rule wanted the
> service back, nothing restores it; the operator has to.
>
> Both of those are surfaced in the UI rather than smoothed over.
>
> Commands come from config/warden.yaml only — never from an API caller — so a request can
> name a configured service id but can never supply a command to run.

That last line is the security model: `POST /api/hosts/services/{sid}` carries only
`{"running": bool}`; the shell string is looked up from config by id.

### Mechanism

- Library: **asyncssh** (`asyncssh==2.24.0`, pinned in `requirements.txt`), imported
  *inside* `available()` and `run()` so a missing library degrades to a message rather than
  breaking startup.
- Auth is **username + password** from environment variables
  (`asyncssh.connect(host.address, port=host.port, username=user, password=password)`). No
  key-based path exists.
- Host-key checking is off by default: `known_hosts=None if not host.known_hosts else ()` —
  the code's reason being that "a LAN NAS rarely has a key in known_hosts, and failing
  closed here would just push people to disable SSH checking globally."
- One command per connection: `conn.run(command, check=False)`, then `conn.close()` in a
  `finally`. `run()` returns `(exit_status, stdout.strip(), stderr.strip())`.
- `HostControl` is constructed once in `warden/main.py` as `ctx.hosts = HostControl(ctx)`.

#### Timeouts

| Constant | Value | Covers |
|---|---|---|
| `CONNECT_TIMEOUT` | 12 s | `asyncio.wait_for` around `asyncssh.connect` |
| `COMMAND_TIMEOUT` | 45 s | `asyncio.wait_for` around `conn.run` |

A timeout raises `asyncio.TimeoutError`, which surfaces as `running: null` with a
`"TimeoutError: "`-prefixed detail on reads, and as a 502 on a write.

### Config shape

Hosts are a **mapping** (the key becomes `HostConfig.name`); managed services are a
**list**.

```yaml
hosts:
  qnap:
    address: 10.7.11.21           # the NAS running Plex
    port: 22
    user_env: QNAP_SSH_USER
    password_env: QNAP_SSH_PASS

managed_services:
  - id: plex_server
    name: "Plex server"
    description: "Stops Plex on the NAS. Absolute — but it stops it for everyone."
    host: qnap
    icon: film
    confirm: true
    start:  "/sbin/qpkg_cli --start PlexMediaServer"
    stop:   "/sbin/qpkg_cli --stop PlexMediaServer"
    status: "/sbin/qpkg_cli --status PlexMediaServer"
    status_running_match: "running"
```

`HostConfig` (`warden/models.py`):

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | str | — | the YAML mapping key |
| `address` | str | — | host or IP |
| `port` | int | `22` | SSH port |
| `user_env` | str | `""` | env var **name** holding the SSH username |
| `password_env` | str | `""` | env var **name** holding the SSH password |
| `known_hosts` | bool | `false` | `false` = don't verify the host key (typical for a LAN NAS) |

`ManagedServiceConfig`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | str | — | the id used by the API, rules and quick actions |
| `name` | str | — | display name |
| `host` | str | — | must match a `HostConfig.name` |
| `description` | str | `""` | shown under the name in the UI |
| `icon` | str | `"server"` | UI icon |
| `start` | str | — | shell command to start it |
| `stop` | str | — | shell command to stop it |
| `status` | str | `""` | optional; its output is matched below |
| `status_running_match` | str | `"running"` | case-insensitive substring meaning "it's up" |
| `confirm` | bool | `true` | ask before stopping (it hits everyone) |

Credentials never appear in `warden.yaml` — only the names of the env vars that hold them.

### How status is determined

`HostControl.status(svc)` runs `svc.status` over SSH and does a case-insensitive substring
test against **stdout and stderr concatenated**:

```python
blob = f"{out}\n{err}".lower()
running = svc.status_running_match.lower() in blob
```

The exit code is not consulted for running-ness; it is only the fallback detail text (`(out
or err or f"exit {code}")[:200]`).

`running` is `None` — state genuinely unknown, never guessed as "stopped" — in four cases:

| Case | `detail` | `configured` |
|---|---|---|
| `svc.host` names no configured host | `unknown host '<name>'` | *(key absent)* |
| asyncssh missing, or user/password env empty | `asyncssh not installed` / `set <USER_ENV> and <PASSWORD_ENV> in .env` | `false` |
| `status:` left blank in config | `no status command configured` | `true` |
| SSH/connect/run raised | `<ExceptionType>: <message>` | `true` |

The dashboard renders `running === null` as a `?` chip reading "state unknown" (or the
detail), rather than as a toggle claiming the service is down.

`list_services()` probes every managed service concurrently (`asyncio.gather(...,
return_exceptions=True)`) and returns, per service: `id`, `name`, `description`, `icon`,
`host`, `confirm`, `running`, `detail`, `configured`. Each probe opens its own SSH
connection, so this read is slower than the rest of the API. (`HostControl` holds a
`_status_cache` that `set_running` invalidates, but nothing populates or reads it — every
probe is live.)

`set_running(svc, running)` runs `svc.start` or `svc.stop`, truncates the output to 300
chars, sets `ok = (exit code == 0)`, writes an audit row (`actor "user"`, kind
`host_service`, target `<id>=running|stopped`), clears the cache entry, logs a warning on
non-zero exit, and returns `{"id", "requested", "ok", "detail"}`. `ok` reflects the
command's exit status only — it does not re-probe to confirm the service actually reached
that state.

### API

All three live in `warden/api/routes_actions.py` under the `/api` prefix.

| Route | Behaviour |
|---|---|
| `GET /api/hosts/services` | `list_services()`; returns `[]` if `ctx.hosts` is `None`. |
| `POST /api/hosts/services/{sid}` | Body `{"running": bool}`. 404 if `sid` is not a configured managed service; on any failure it audits `host_service` `ok=false` and returns **502** with the exception text. On success it pins a manual hold and calls `ctx.after_change("host_service")`. |
| `GET /api/hosts/{name}/discover` | 404 if `name` is not a configured host; 400 with the `available()` reason if asyncssh or credentials are missing; otherwise the probe report below. |

#### `discover` — the first-run helper

The reason, from the docstring: "A QNAP may run Plex as a QPKG (`qpkg_cli`) or as a
Container Station container (`docker`), and the right command differs. Rather than guess,
look."

It runs seven labelled probes in order — `uname` (`uname -a`), `qpkg_cli` (`command -v
qpkg_cli || ls /sbin/qpkg_cli`), `qpkg_list` (`/sbin/qpkg_cli --list | head -40`), `docker`
(`command -v docker`), `docker_ps` (`docker ps --format '{{.Names}}\t{{.Image}}' | head
-20`), `systemctl` (`command -v systemctl`), `initd` (`ls /etc/init.d | head -40`) — and
returns `{label: {"exit": int, "out": str[:1500], "err": str[:200]}}`. A probe that raises
becomes `{label: {"error": "<Type>: <msg>"}}` and **stops the run** ("connection problem: no
point running the rest"), so a failed report is short by design. There is no UI button; it
is a curl/first-run tool, and the `managed_services` comment in `warden.yaml` tells you to
swap in the docker form (`docker start plex` / `docker stop plex`, status `docker inspect -f
'{{.State.Status}}' plex`) if that is what it finds.

### Environment variables

For the shipped config: **`QNAP_SSH_USER`** and **`QNAP_SSH_PASS`**, both in `.env` (listed
in `.env.example`). Generally, whatever `user_env` / `password_env` name.

`credentials()` returns `""` for an env var that is unset *or* for a blank
`user_env`/`password_env` in config, and `available()` then returns `(False, "set
QNAP_SSH_USER and QNAP_SSH_PASS in .env")`. Consequences when unset:

- `GET /api/hosts/services` still succeeds: the service comes back `running: null`,
  `configured: false`, with that message as `detail`, and the card shows `?` — nothing
  pretends to be controllable.
- `run()` (and so `set_running`) raises `RuntimeError(why)`; `POST` turns that into a 502
  and an `ok=false` audit row.
- `discover` returns 400 with the same message.

Missing asyncssh behaves identically, with `detail` = `"asyncssh not installed"`.

### Manual holds

A managed service participates in the same hold ("pin") mechanism as the per-group AdGuard
switches, using the reserved group name **`_host`** and the service id:
`ctx.db.pin_switch("_host", sid, "running"|"stopped", "user", _pin_expiry(ctx))`.
`_pin_expiry` is the next 04:00 local — a backstop only; the real release is a scheduled
rule writing that switch.

The comment at the pin site states the intent: *"a manual start/stop of a whole-house
service is a hold like any switch flip: the dynamic evaluator must not flip it back (movie
night stays a movie night)"*.

Held services are enforced in three places:

- `RuleEngine._execute` (`set_host_service`): when `fired_by == "signal"`, a hold on
  `("_host", service)` returns the note `"spared: manual hold"` and nothing runs; when
  `fired_by == "reset"` (cron, or a human pressing Run now) the pin is cleared **before**
  the write.
- `RuleEvaluator.apply`: a held service is skipped, recorded as `host:<id>` in the withheld
  list, and audited — so "why didn't the rule stop Plex?" has a visible answer.
- The dashboard card shows a "✋ manual" badge sourced from `store.holds["_host/<id>"]`.

### The `set_host_service` rule action

Schema (`warden/rules/schema.py`): `{"kind": "set_host_service", "service": "<managed
service id>", "running": <bool>}` — part of the discriminated `Action` union, so it is
available to both the compiled-AST engine and the LLM evaluator.

- **Compilation**: `build_vocabulary` exposes every managed service as `host_services` (id,
  name, description); `Vocabulary.prompt_reference()` prints them under a header warning
  they are "real processes; stopping one affects EVERYONE in the house, not a group — only
  use when the rule clearly means the service itself."
- **Evaluation**: `RuleEvaluator.action_catalog` offers one entry per managed service with
  the note "GLOBAL — stops/starts the actual service for EVERYONE in the house and ends
  anything mid-stream. Only use it if the rule explicitly asks for it." `_parse_actions`
  accepts the action only if `config.managed_service(sid)` resolves, so a hallucinated id is
  dropped silently.
- **Execution**: `RuleEngine._execute` raises `ValueError(f"unknown managed service:
  {sid}")` for an unknown id and `RuntimeError("host control not available")` if `ctx.hosts`
  is `None`; both are caught by `run_rule` and audited `ok=false`. The audit description is
  `"<id>=running|stopped (whole house)"`.
- **Quick actions**: a `QuickActionStep` may carry `host_service` + `running` instead of
  `service`/`group`. Such a step pins `_host/<id>` the same way, and — per the code comment
  — is "global, so never auto-reverted": `revert_after_minutes` snapshots only the AdGuard
  switches. A step whose id is unknown, or with `ctx.hosts` unset, is skipped rather than
  failing the action.


---

## Configuration

Warden reads two files, each exactly once, in `build_app()` (`warden/main.py:58-60`):
`config/warden.yaml` for structure, `.env` for secrets and server settings. Neither is
watched. The YAML's own header says "the app hot-reloads it" — the code has no reloader
(`load_config` is called once in `build_app`; the only other call site is
`compiler.py`'s `__main__` self-check), so an edit needs a restart.

`load_dotenv()` runs with the default `override=False`, so a variable already in the real
environment beats the one in `.env`.

### Part 1 — `config/warden.yaml`

Eleven top-level keys, all parsed by `warden/config.py:load_config`. `groups`, `sources` and
`hosts` are maps keyed by name and flattened into lists (the key becomes `name`, or `key`
for a source); everything else is a list. The models are pydantic v2 with the default
`extra="ignore"`, so a misspelled field is dropped silently rather than rejected.

| Key | Shape | Purpose |
| --- | --- | --- |
| `adguard` | map | AdGuard address — display only (see below) |
| `groups` | map of name → group | Device groupings, tag membership, baseline, default block list |
| `clients` | list | Devices named explicitly, each pinned to a group |
| `services` | list | The on/off-able services, per group |
| `subjects` | list | People, with date-driven contexts |
| `sources` | map of key → source | Pluggable scrapers: adapter, cron, secrets, options |
| `signals` | list | The vocabulary rules and the compiler can name |
| `rules` | list of strings | Plain-English rules seeded on first boot |
| `quick_actions` | list | One-tap Dashboard presets |
| `hosts` | map of name → host | Machines reachable over SSH |
| `managed_services` | list | Services on those hosts Warden can start/stop |

#### `adguard`

```yaml
adguard:
  url: http://10.7.11.29     # string, default http://10.7.11.29
```

`AdGuardConfig` has exactly one field. **It is not used to connect.** `AdGuardService` takes
its address from `Settings.adguard_url` (`ADGUARD_URL`, code default `http://10.7.11.29`);
the YAML value is echoed by `GET /api/config` and no front-end code reads it. The file's
comment "overridden by `ADGUARD_URL` if set" is backwards — the env value (or its code
default) is always the one used.

#### `groups`

```yaml
groups:
  kids:
    tag: user_child                       # informational; Warden never writes tags
    match_tags: [user_child]              # ALL must be present for tag membership
    baseline: { safe_search: true, parental: true, safebrowsing: true }
    default_blocked: [youtube]            # service ids
```

Membership has two routes, and a device joins at most one group:

* **Explicit** — a `clients:` entry names the group. It wins.
* **Tag-based** — an AdGuard client joins a group when it carries *every* tag in
  `match_tags`. Groups are tried most-specific-first (most tags wins; config order breaks
  ties), so `[user_child, device_tablet]` claims a child's tablet ahead of `[user_child]`
  (`warden/adguard/service.py:106-136`). A device with no tags is skipped.

`tag` reaches `GroupState.tag`, `GET /api/config` and the fake backend's client records only
— Warden never writes tags to AdGuard, so the tag has to be applied there by hand.

`baseline` is read **only by the fake backend** (`warden/adguard/fake.py:32-43`). Nothing
writes `safe_search` / `parental` / `safebrowsing` to a live AdGuard, so in live mode these
three flags do nothing.

`default_blocked` is documented in the file as "services blocked when a device is first
created", but `AdGuardClient.add_client` has no caller — Warden never creates AdGuard
clients. What it actually does: seeds the fake backend's clients, and supplies the displayed
state for a group with no provisioned device (`service.py:250`).

#### `clients`

```yaml
clients:
  - { name: "Asta iPad", group: kids, ids: ["10.7.11.31"], subject: asta }
```

`name` must match the AdGuard client name exactly — that is the only key AdGuard writes are
addressed by. `ids` are not purely cosmetic despite the file's comment: they seed the fake
backend's clients and map a request's socket address to a device name in the disable-reason
audit line (`routes_actions.py:377`). `subject` is optional and links the device to a
person. `discovered` exists on the model but is set by the tag-matching code, not by config.

#### `services`

```yaml
services:
  - { id: youtube, name: YouTube, icon: youtube, groups: [kids, tv] }
```

`id` must be a valid AdGuard blocked-service id. `groups` decides which group cards get the
toggle; a service listed in no group is never written. `icon` defaults to `globe` and is
looked up in a fixed inline SVG set — `globe, youtube, film, music, gamepad, message,
shield, play, stop, zap, clock, edit` — with anything unrecognised falling back to `globe`.

Warden only ever adds and removes the ids listed here, which is why permanently-blocked
services (TikTok, Roblox, Discord) are deliberately absent: listing one would hand it back
on the next group-wide unblock.

#### `subjects`

```yaml
subjects:
  - id: luke
    name: Luke Dabbs
    contexts:                                        # optional, date-driven
      - { label: "Jellyfish Class", effective_to:   "2026-09-02" }
      - { label: "Swordfish Class", effective_from: "2026-09-02" }
```

Contexts resolve by today's date to the subject's current label (`registry.py:333`,
`reminders.py:252`) and are passed to the dynamic evaluator. In the reminders path the
portal's own class name wins and config contexts are the offline fallback.

#### `sources`

```yaml
sources:
  atom:                                 # map key -> SourceConfig.key
    adapter: atom                       # package under warden/adapters/<name>/adapter.py
    display_name: "Atom Learning"
    enabled: true
    schedule: "0 6-21 * * *"            # cron, in Settings.tz
    subject: luke
    secrets: {}                         # logical name -> ENV VAR name
    options: { ... }                    # opaque, adapter-specific
```

`SourceRegistry.start()` imports `warden.adapters.<adapter>.adapter:Adapter` for every
entry, then schedules only those where `enabled` **and** `schedule` are both set and the
import succeeded. Nothing runs on boot — a run is a cron tick, an operator "run now", or a
rule evaluation that needs data the schedule says is already due. A broken adapter is
recorded in `_errors` and does not sink the others.

`secrets` maps a logical name to an environment variable name; `resolve_secrets()` reads
each with a default of `""`. Nothing sensitive lives in the YAML.

`options` is passed through untouched as `SourceContext.options`. Recognised keys, with the
defaults **in the adapter** (the YAML sets most of them explicitly):

**atom** — `api_base` (`https://api.atomlearning.com`), `state_file`
(`data/atom_state.json`), `student_id` (blank = auto-discover), `tz` (`Europe/London`),
`daily_target_islands` (`1`, used only when a week has no target at all),
`weekly_island_target` (no default; fallback for a run that could not read Atom's plan),
`require_assignments_done` (`true`), `year_group` / `id_course` (read from the profile and
active enrolment unless pinned). `app_base` is set in the YAML but **no adapter code reads
it** — only `warden.adapters.atom.login` has an `--app-base` flag of its own.
`weekly_island_target_origin` is injected at run time by the registry from the stored UI
override; do not set it in YAML.

**weduc** — `api_base` (`https://app.weduc.co.uk`), `ui_base`
(`https://ui.app.weduc.co.uk`), `state_file` (`data/weduc_state.json`), `user_id`,
`child_id`, `entity_id`, `inbox_folder_id` (each with a hard-coded fallback matching this
account), `files_dir` (`data/weduc_files`), `cache_extensions` (`pdf`, comma-separated),
`max_attachment_mb` (`60`), `meals_days_back` (`7`), `meals_days_ahead` (`35`). Recognised
but **not set in the shipped YAML**: `calendar_days_back` (`365`), `calendar_days_ahead`
(`120`), `school_lookahead_days` (`21`), `school_scan_days` (`120`), `tz` (`Europe/London`).
`pdf_dpi` (`140`) and `pdf_max_pages` (`12`) are read from this source's options by
`warden/documents.py`, not by the adapter.

**parentpay** — `state_file` (`data/parentpay_state.json`), `weduc_state_file`
(`data/weduc_state.json`), `entity_id`, `child_id` (Weduc ids, used to mint the SSO token),
`consumer_id` (the ParentPay id from the ClubsCalendar URL), `clubs` (list of `{id, name,
kind}`; `kind` defaults to `club` and is what a rule reasons with), `days_ahead` (`21`),
`tz` (`Europe/London`). There is no ParentPay password — its `secrets` point at the same
`WEDUC_USERNAME` / `WEDUC_PASSWORD` pair, because the session is minted from the Weduc one
over partner-login SSO.

`warden/adapters/amazon/` exists but is not named by any source block, so it is never
imported — recon only, no `adapter.py`.

#### `signals`

```yaml
signals:
  - key: "atom.luke.daily_complete"
    type: bool                        # bool | number | string, default bool
    source: atom
    subject: luke
    describe: "Luke has done his set Atom work and practised today"
```

This is vocabulary, not a schema: it feeds the rule compiler (`build_vocabulary`), the
evaluator's `describe` lookup, and the UI's simulate list. It does not constrain what an
adapter may emit. `type` does one concrete job — `POST /api/signals/emit` coerces the
incoming JSON value to the declared type so a bool signal is a real bool, which the bus's
edge detection depends on; an undeclared key coerces to bool.

#### `rules`

```yaml
rules:
  - "At 8pm every day, disable all kids devices"
```

Seeds only: `_seed_rules` returns immediately if the rules table has any row, so this list
is compiled once, on first boot, into ids `seed-1`…`seed-N`. Each is tried against the
deterministic parser first and only falls through to the LLM backend if the parser raises —
first boot stays fast and offline. A rule that fails to compile is still stored, with the
error surfaced in the UI.

#### `quick_actions`

```yaml
quick_actions:
  - id: streaming_on_2h
    label: "Streaming on (2h)"
    description: "..."                  # tooltip + confirm text
    icon: play                          # default "zap"
    style: good                         # default | good | warn | danger
    confirm: false                      # ask before firing
    revert_after_minutes: 120           # snapshot the touched switches, restore later
    steps:
      - { service: youtube, state: allowed }              # every group exposing it
      - { service: netflix, groups: [tv], state: allowed } # narrowed to those groups
      - { group: kids, state: blocked }                    # that group's whole list
      - { host_service: plex_server, running: false }      # start/stop for real
```

Four step forms, resolved by `_expand` (`routes_actions.py:161`) except the last:

* `service` alone fans out to every group in that service's `groups` — that is what
  "everywhere" means.
* `service` + `groups` (or `group`, singular — both are honoured) narrows to those groups,
  and only where the service is actually listed for the group.
* `group` alone covers that group's whole managed service list.
* `host_service` + `running` starts or stops a real service. Handled in a separate loop
  after the switch flips, because it is global and is therefore **never auto-reverted**; the
  flip is pinned as a manual hold so the evaluator leaves it alone.

Unknown ids are skipped rather than failing the action. Duplicate `(group, service)` pairs
de-duplicate with last-write-wins. `revert_after_minutes` snapshots only the switches about
to be touched, so the restore puts back what was there rather than imposing a baseline.

One trap: `_expand` ignores `host_service` steps, and `run_quick_action` raises 400
("resolves to no valid switches") when `_expand` returns nothing — so a quick action built
*only* from `host_service` steps is rejected before the host-service loop runs. No shipped
action hits this.

#### `hosts`

```yaml
hosts:
  qnap:
    address: 10.7.11.21
    port: 22                       # default 22
    user_env: QNAP_SSH_USER        # env var holding the username
    password_env: QNAP_SSH_PASS    # env var holding the password
    # known_hosts: false           # default false = do not verify the host key
```

Credentials are never in the YAML — only the *names* of the variables that hold them.
`HostControl.available()` reports the host unusable, naming both variables, when either is
empty or `asyncssh` is missing. `known_hosts: false` (the default, and what the shipped
config uses by omission) passes `known_hosts=None` to asyncssh, i.e. no host-key check —
deliberate, because a LAN NAS rarely has a key and failing closed pushes people to disable
SSH checking globally.

#### `managed_services`

```yaml
managed_services:
  - id: plex_server
    name: "Plex server"
    description: "..."
    host: qnap                     # HostConfig.name
    icon: film                     # model default "server"
    confirm: true                  # model default true
    start:  "/sbin/qpkg_cli --start PlexMediaServer"
    stop:   "/sbin/qpkg_cli --stop PlexMediaServer"
    status: "/sbin/qpkg_cli --status PlexMediaServer"    # optional
    status_running_match: "running"                       # case-insensitive substring
```

The escape hatch for what DNS filtering cannot reach: a media server on your own LAN answers
on its IP whatever the resolver says. Commands come from this file only, never from an API
caller, so a request can name a configured id but can never supply a command.
`status_running_match` is matched case-insensitively against stdout+stderr; with no `status`
command the UI shows the state as unknown. These are **global** — a stop is a stop for
everyone, and anything mid-stream ends — which is why they are modelled apart from the
per-group toggles and shown in their own "Whole-house services" row.

Note the model's default `icon: "server"` is not in the UI's icon set, so it renders as a
globe; the shipped entry sets `film`.

### Part 2 — environment

Every `os.getenv` / `os.environ` read under `warden/`. Eighteen of them land in `Settings`
(`warden/config.py:55`); the rest are read at their point of use.

| Env var | Settings field | Default in code | `.env.example` | Notes |
| --- | --- | --- | --- | --- |
| `ADGUARD_URL` | `adguard_url` | `http://10.7.11.29` | `http://10.7.11.29` | The address actually used; the YAML `adguard.url` is not |
| `ADGUARD_USER` | `adguard_user` | `""` | `cliff` | **Differs** |
| `ADGUARD_PASS` | `adguard_pass` | `""` | `change-me` | **Differs** |
| `ADGUARD_MODE` | `adguard_mode` | `auto` | `auto` | `auto` probes; live needs both creds *and* a successful `/control/status` |
| `ANTHROPIC_API_KEY` | `anthropic_api_key` | `""` | *(empty)* | Passed explicitly to `AsyncAnthropic`; document reading requires it (the CLI path cannot do it) |
| `WARDEN_LLM` | `llm_backend` | `auto` | `auto` | `auto \| api \| cli \| off` |
| `WARDEN_CLAUDE_CLI` | `claude_cli` | `claude` | `claude` | Resolved with `shutil.which` when deciding the backend |
| `WARDEN_LLM_MODEL` | `llm_model` | `claude-haiku-4-5-20251001` | same | |
| `WARDEN_EVAL_MODEL` | `eval_model` | `""` (= use `llm_model`) | `claude-sonnet-5` | **Differs** — the shipped example points the dynamic evaluator at a stronger model |
| `WARDEN_HOST` | `host` | `0.0.0.0` | `0.0.0.0` | Also set in the Dockerfile |
| `WARDEN_PORT` | `port` | `8080` | `8080` | Parsed with `int()` — a non-numeric value raises at startup |
| `WARDEN_DB` | `db_path` | `warden.db` | `warden.db` | Relative to the process CWD. Dockerfile sets `/app/data/warden.db`; see below |
| `WARDEN_TZ` | `tz` | `Europe/London` | `Europe/London` | Timezone for both schedulers |
| `WARDEN_EVAL_INTERVAL_MIN` | `eval_interval_min` | `10` | `60` | **Differs** — safety-net tick; dynamic rules normally schedule their own next check |
| `WARDEN_COLLECT_WAIT_SEC` | `collect_wait_sec` | `180` | `180` | How long an evaluation waits for a running or overdue collection before deciding without it |
| `WARDEN_USERNAME` | `auth_username` | `admin` | `admin` | |
| `WARDEN_PASSWORD` | `auth_password` | `""` | *(empty)* | Empty disables the login gate entirely |
| `WARDEN_SECRET` | `auth_secret` | `""` | *(empty)* | Cookie-signing key; empty derives it from `username:password`, so a password change logs everyone out |
| `WEDUC_USERNAME` | — | `""` | *(empty)* | Named by `secrets:` on both the weduc and parentpay sources; also read directly by `warden/adapters/weduc/login.py` and `warden/adapters/parentpay/login.py` |
| `WEDUC_PASSWORD` | — | `""` | *(empty)* | As above |
| `QNAP_SSH_USER` | — | `""` | *(empty)* | Read via `HostConfig.user_env` in `warden/hosts.py:44` |
| `QNAP_SSH_PASS` | — | `""` | *(empty)* | Read via `HostConfig.password_env` |
| `ProgramFiles`, `ProgramFiles(x86)`, `LocalAppData` | — | — | — | Windows only, `warden/adapters/atom/login.py:48` — locating Chrome or Edge for the one-off Atom session capture. Not configuration |

The source-secret and host-credential variable names are not fixed by code: they are
whatever `sources[].secrets` and `hosts[].{user_env,password_env}` name in the YAML. The
four above are what the shipped config names.

#### Where `.env.example` differs from the code default

* `ADGUARD_USER=cliff` / `ADGUARD_PASS=change-me` — the code defaults are empty, which makes
  `_probe_live()` skip the probe outright and stay on the fake backend. With these
  placeholders present the probe runs, fails to authenticate, and falls back to fake anyway
  — the same outcome, one wasted HTTP call, and a "creds are configured" appearance that is
  false.
* `WARDEN_EVAL_MODEL=claude-sonnet-5` — the code default is empty, meaning the evaluator
  shares `llm_model` (Haiku). Copying the example silently upgrades the dynamic evaluator to
  a stronger, costlier model. That is intentional (the evaluator both judges the rule and
  computes its own next wake-up), but it is a behaviour change relative to the code default.
* `WARDEN_EVAL_INTERVAL_MIN=60` — the code default is `10`. Copying the example makes the
  safety-net sweep six times less frequent.
* `WARDEN_DB=warden.db` — matches the code default but **contradicts the Dockerfile**, which
  sets `/app/data/warden.db` so the SQLite state lands in the `./data` bind mount.
  `docker-compose.yml` uses `env_file: .env`, and Compose's env_file takes precedence over
  the image's `ENV`, so a `.env` copied verbatim puts the database at `/app/warden.db` —
  inside the container layer, outside the volume, lost on rebuild. Either comment the line
  out or set it to `/app/data/warden.db`.


---

## Persistence

One SQLite file holds everything Warden remembers. Path from `WARDEN_DB`
(`Settings.db_path`, default `warden.db`); `DB.__init__` opens a single connection with
`check_same_thread=False`, sets `row_factory = sqlite3.Row` and `PRAGMA journal_mode=WAL`,
then runs the schema and `_migrate` under the write lock. The lock (`threading.Lock`) is
held for writes only — reads run unguarded — because APScheduler fires jobs on worker
threads while FastAPI serves reads on the event loop. Pydantic models are stored as JSON
text, timestamps as ISO-8601 strings.

Every method in `db.py` has a caller. The only unused thing in the module is the
`SignalType` import.

### Tables

`_SCHEMA` is re-run on every boot as `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT
EXISTS`.

**`rules`** — one row per rule, natural-language text plus its compiled form. Written by
`upsert_rule`, stamped by `touch_rule` after a fire.

| Column | Type | Purpose |
|---|---|---|
| `id` | TEXT PK | rule id |
| `text` | TEXT NOT NULL | the rule as written |
| `enabled` | INTEGER default 1 | |
| `compiled` | TEXT | `CompiledRule` JSON; NULL if it never compiled |
| `compile_error` | TEXT | why it didn't |
| `source` | TEXT default `'user'` | who authored it |
| `created_at`, `updated_at` | TEXT | `upsert_rule` sets `created_at` once, `updated_at` every write |
| `last_fired_at`, `last_result` | TEXT | last evaluation |
| `next_check_at`, `next_check_reason` | TEXT | when a sleeping dynamic rule next looks, and why |

**`signals_latest`** — current value per signal key (`key` PK, `payload` = `Signal` JSON,
`updated_at`). `SignalBus` warms its in-memory cache from this at boot.

**`signal_history`** — append-only, one row per emit (`id`, `key`, `payload`, `at`); index
`ix_sighist_key` on `(key, id DESC)`. Read newest-first by `signal_history(key, limit=50)`.

**`audit`** — the Activity feed. `id`, `at`, `actor`, `action`, `target`, `detail`, `ok`,
`ref`. See below.

**`llm_calls`** — every model exchange verbatim.

| Column | Type | Purpose |
|---|---|---|
| `id` | INTEGER PK | the id an audit line's `ref` points at |
| `at` | TEXT NOT NULL | |
| `purpose` | TEXT | `rule_compile`, `rule_eval:<id>`, `reminders`, `newsletter` |
| `backend`, `model` | TEXT | `anthropic-api` / `claude-cli`, and the model id |
| `system`, `prompt`, `response` | TEXT | exactly what was sent and received (clipped — see retention) |
| `ok` | INTEGER NOT NULL default 1 | |
| `ms`, `input_tokens`, `output_tokens` | INTEGER | |
| `error` | TEXT | |

**`source_runs`** — one row per adapter collection: `id`, `source`, `started_at`,
`finished_at`, `ok`, `item_count`, `signal_count`, `detail`. `start_run` inserts with
`ok=0`; `finish_run` closes it. `SourceRun.state_changed` is *not* a column — it is set only
on the object a live run returns.

**`items`** — normalised adapter output. `external_id` TEXT PK, `source_id`, `payload`
(`Item` JSON), `fetched_at`; index `ix_items_source` on `(source_id, fetched_at DESC)`.
`save_items` uses `INSERT OR IGNORE` and returns the number of genuinely new rows, so a
re-collected item keeps its first-seen payload and is never updated. `external_id` is the
whole primary key, so it must be unique across sources, not just within one.

**`kv`** — `k` TEXT PK, `v` TEXT. See namespaces below.

**`state_history`** — archived "state of play" snapshots: `id`, `source` NOT NULL, `at`,
`hash`, `payload`; index `ix_statehist` on `(source, id DESC)`.

**`switch_pins`** — a parent's manual flip of one switch: `group_name`, `service`, `state`,
`actor` (default `'user'`), `created_at`, `expires_at`, PK `(group_name, service)`.

**`overrides`** — a standing per-device exemption: `client` TEXT PK, `kind` NOT NULL default
`'unblock'`, `created_at`, `expires_at` (NULL = forever), `actor` default `'user'`.

### The kv store

Five namespaces, all flat `prefix:…` keys with JSON or raw-string values. Nothing enumerates
them — every read is by exact key.

| Key | Value | Written by | Read by |
|---|---|---|---|
| `cursor:<source>` | opaque `next_cursor` string from the adapter | `_collect_and_store`, when the adapter returns one | `_build_context` → `SourceContext.since_cursor` |
| `state:<source>` | the source's latest "state of play", whole | `_collect_and_store`, every run | dynamic evaluator, `/api/sources` (`data_quality.live`), today's-share and published-target lookups, reminders (`state:weduc`, `state:parentpay`) |
| `target:<source>:<subject>:<YYYY-MM-DD>` | `{islands, note, set_at}` — a parent's weekly override, keyed on the Monday | `set_target` | `get_target`; `clear_target` calls `del_kv` so the source's own published plan takes over again |
| `doc:<attachment-id>` | the LLM digest of one newsletter PDF | `DocumentReader.process_items` after a successful read | `get_cached` — presence is what stops a 28 MB newsletter being re-read and re-charged |
| `reminders:weduc:<16 hex>` | the model's reminder reading, plus `_model` | `ReminderBuilder._llm_reminders` | same, on the next page build |

The reminders key hashes the message pool itself (`id`, `sent`, first 200 chars of `body`,
plus the child's class) and deliberately **not** today's date — `_verify` re-applies the
"not in the past" test on every build, so yesterday's reading is still correct today and
midnight doesn't force a fresh call. A new message, or an old one ageing out of the lookback
window, changes the hash.

`state:<source>` is the *latest* state, overwritten every run; `state_history` is the
archive. Both are written in the same block.

### Migration path

`CREATE TABLE IF NOT EXISTS` will not add a column to a table that already exists, so
`_migrate` runs after the schema: for each table in a `wanted` dict it reads `PRAGMA
table_info` and `ALTER TABLE … ADD COLUMN` for anything missing. Cheap, idempotent, and it
fails at startup rather than at query time on an upgraded `warden.db`.

Currently migrated: `rules.next_check_at`, `rules.next_check_reason`, `audit.ref` — all
TEXT. A `sqlite3.Error` reading a table's info skips that table rather than aborting boot.

There is no version stamp, no down path and no backfill — an added column is NULL on
existing rows. Two belt-and-braces guards cover a row read before `_migrate` could run:
`DB._col` returns None for a missing column when building a `Rule`, and `list_audit` tests
`"ref" in r.keys()` before using it.

### Retention and trimming

The LLM log is the only table trimmed automatically, because a world-state prompt runs to
tens of KB.

- **How many.** `LLM_LOG_KEEP = 300`. Every `add_llm_call` follows the insert with `DELETE
  FROM llm_calls WHERE id <= (SELECT MAX(id) FROM llm_calls) - 300`. The window is by id,
  not by row count.
- **Per field.** `LLM_FIELD_CHARS = 200_000`, applied by `clip()` to `system`, `prompt` and
  `response` only. `error` is stored whole.
- **What a truncation looks like.** The first 200,000 characters, then `\n\n… [truncated:
  <original length> characters in total]`. Marked in the text rather than done silently, so
  a reader can tell "this is all of it" from "this is the start of it".
- **Listings.** `list_llm_calls` never ships bodies; it substitutes `[N characters]` for
  each, where N is `length()` of the *stored* text — a clipped field therefore reports ~200k
  plus the marker, not its original size.
- **After the trim.** `GET /api/llm/{id}` returns 404 for a trimmed call ("the log keeps the
  recent ones") while its audit line survives — the summary outlives the transcript by
  design.

Nothing else is trimmed. `audit`, `signal_history`, `items`, `source_runs`, `state_history`
and `kv` grow without bound; there is no VACUUM anywhere. The only other deletes are
purposeful: `switch_pins` self-pruning (below), `delete_override`, `delete_rule`, and
`del_kv` for a cleared weekly target.

### An audit entry, and the link to a transcript

`AuditEntry`: `id`, `at` (defaults to now), `actor`, `action`, `target`, `detail`, `ok`,
`ref`. `actor` is `system` | `user` | `scheduler` | `evaluator` | `rule:<id>` | `source:<key>` |
`weduc` | `llm` | …; `action` is the
verb (`set_service`, `set_group`, `source_run`, `llm_call`, `override.expired`, …).
`ctx.audit()` writes the row and publishes `{"type": "audit", "entry": …}` on the EventHub,
so open browsers get the line without polling. `list_audit` returns newest-first.

`ref` is a pointer to a bigger record the line summarises. The only form in use is
`llm:<id>`:

1. `ctx.record_llm(call)` stores the exchange with `add_llm_call` and gets its id back.
2. It composes the one-line detail — model, backend, `N in / M out` when tokens are known,
   `X.Xs`, plus `— <error>` on failure.
3. It writes the audit line as actor `llm`, action `llm_call`, target `call.purpose or
   "llm"`, `ok=call.ok`, `ref=f"llm:{call_id}"`.
4. The feed row (`app.js auditRow`) matches `/^llm:(\d+)$/` on `ref` and draws a `prompt ⤢`
   button; clicking it fetches `GET /api/llm/{id}` and shows the system prompt, the message
   sent and the reply received. The feed carries only the id — never the text.

If the store itself fails, `record_llm` logs and returns 0 rather than raising: the model
call has already happened, and bookkeeping must not break the work it describes.

### state_history and what counts as a genuine change

`record_state_snapshot(source, state) -> bool` appends only when the facts moved.

- The hash is SHA-1 of `json.dumps(_without_clocks(state), sort_keys=True, default=str)`.
- `_CLOCK_FIELDS` — `now`, `collected_at`, `fetched_at`, `generated_at` — are stripped
  recursively through dicts and lists before hashing. An adapter re-stamps `now` at the top
  and `collected_at` inside `data_quality` on every collection; with those in the hash, an
  hour of identical Atom data archived 15 times a day.
- Only the **previous** row for that source is compared. This is "differs from the last
  snapshot", not "never seen before" — an A→B→A cycle appends three rows.
- The stored `payload` is the state *whole*, clocks included; stripping is for the
  comparison only.

The return value is load-bearing beyond the archive. On True, `SourceRegistry` sets
`_changed_since_eval[key] = True`, which becomes `evaluate_all(on_data_change=True)` and
wakes rules that were asleep until their own next check; the flag is cleared only once a
sweep actually runs (a skipped sweep leaves it set, so a second identical scrape can't
swallow the wake). "Always changed" is the same as "never changed" for that purpose, which
is why the clock stripping matters. A target change calls `refresh_after_target_change`,
which sets the flag by hand — the data didn't move, but the bar it is measured against did.

`state_history(source, limit=200)` returns `[{at, state}]` newest-first, served by `GET
/api/sources/{key}/history`.

### switch_pins

A pin records that a parent flipped one switch by hand. Key is `(group_name, service)`;
`_host` is the pseudo-group for host services started or stopped over SSH.

**Precedence.** While a pin is live the dynamic evaluator may not change that switch: a held
`set_service` is withheld and named in the audit line, and a held `set_group` is expanded to
the group's *unheld* switches so the rest still apply. A scheduled rule fired by `"signal"`
spares holds the same way; one fired by `"reset"` clears the pin and writes — the nightly
switchoff is the reset. The parent's hand outranks the model, enforced in code rather than
merely requested in the prompt.

**Expiry.** `expires_at` is supplied by the caller; `routes_actions._pin_expiry` computes
the next 04:00 in `settings.tz`, converted to UTC ISO. It is only the safety net for a
switch no schedule ever touches. It is read on every `/api/state`, every WS state push, every
signal-fired engine action and every evaluator pass — but a **reset**-fired rule (cron, or Run
now) deliberately does not read it, clearing pins instead, which is what makes the 8pm
switch-off the daily reset. `active_pins(now_iso)` deletes `expires_at <= now` before
reading, so the table self-prunes on every read — and it is read on every `/api/state`,
every WS state push, every engine reset and every evaluator pass.

**Clearing.** `clear_pins(pairs)` returns how many existed. Called when a manual write fails
(unpin, so a switch that never moved isn't held), by reset-fired rules, and by a quick
action's timed revert — which skips any switch whose pin `created_at` is newer than the
revert's `armed_at`, on the grounds that someone changed their mind by hand in the meantime.

Pins are created *before* the AdGuard write, so the WS push inside `_mutate` already carries
the hold and the badge appears with the flip.

### overrides

An override exempts **one device** from Warden's group-level writes, so a scheduled rule
can't silently undo a parent's manual unblock. One row per client, `kind` always `"unblock"`
in practice.

**Enforcement.** `main.py` wires `ctx.adguard.active_overrides = lambda:
db.active_override_names(utcnow().isoformat())` — a callable, not a set, so every write sees
the live answer. `AdGuardService._apply` skips exempt names on each group write.
`set_device_services` deliberately ignores overrides: it is the lever used when the
exemption is being created or lifted, and must not be blocked by itself.

**Expiry.** `expires_at IS NULL` means forever, until cancelled — `active_override_names`
matches `IS NULL OR > now`, and `expired_overrides` matches `IS NOT NULL AND <= now`, so a
forever override is never swept. Two different clocks set it: the Devices page offers `2h` /
`4h` / `24h` / `forever`, while a manual per-device *allow* through `/api/actions/client`
uses the same next-04:00 as a switch pin. A manual *block* through that route deletes any
standing override.

**The sweep.** `OverrideKeeper` runs every 60s: take `expired_overrides(now)`, delete the
row **first** (while it stands the device is exempt, and the re-block goes through the very
service the exemption guards), re-block via `set_device_services`, audit `override.expired`
(`ok=False` if the re-block failed), then `after_change`. Deliberately dumb and idempotent —
if Warden is down when an override expires, the next sweep after boot catches it.

Overrides are written before the AdGuard call and deleted again if that call fails, so a
failed unblock never leaves a phantom exemption.
