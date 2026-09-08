"""SourceRegistry — discovers pluggable adapters and drives their runs.

This is the "hub" side of the source-adapter contract (docs/sourceadapterCONTRACT.md
§4): it knows nothing source-specific. For each `config.sources` entry it imports the
named adapter package, assembles a `SourceContext` per run, and orchestrates the
mechanical pipeline:

    start_run → authenticate → collect → save_items → emit signals → store cursor
             → finish_run → audit → after_change

Signals are emitted onto the bus (which drives the rules engine) and pushed to the UI.
Enabled sources are scheduled on their cron cadence, but nothing runs on boot — a run
is always a schedule tick, an operator/API "run now", or a rule evaluation that needs
data the schedule says should already be here (see `ensure_fresh`).

ORDERING: collection completes before rules are evaluated, always. Dynamic rules read
the stored "state of play", so an evaluation that overlaps a collection is judging the
PREVIOUS cycle's data — which shows up as the rule acting a whole cadence late.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from croniter import croniter

from ..config import resolve_secrets
from ..context import AppContext
from ..models import SourceConfig, SourceRun, utcnow
from .base import SourceAdapter, SourceContext

log = logging.getLogger("warden.sources")

# A poll this close to firing counts as due now. Without it a rule waking on the hour
# can win the race against the source job scheduled for the same instant, read the
# previous hour's state and act on it.
_DUE_LEAD = timedelta(seconds=5)


def _playwright_available() -> bool:
    """True if the Playwright package is importable (browsers may still be missing;
    the adapter falls back to fixtures on any real launch failure)."""
    try:
        return importlib.util.find_spec("playwright.async_api") is not None
    except Exception:
        return False


class SourceRegistry:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._adapters: dict[str, SourceAdapter] = {}
        self._fixtures: dict[str, Path] = {}
        self._errors: dict[str, str] = {}
        self._sched = AsyncIOScheduler(timezone=ctx.settings.tz)
        # one shared collection per source, so a cron tick, a waiting rule and a "run
        # now" that land together join the same run instead of racing three scrapes
        self._inflight: dict[str, asyncio.Task] = {}
        # "facts moved since the dynamic rules last got a full pass". Set by every
        # collection that archives a snapshot, consumed by the next evaluation sweep.
        # Needed because the collection that observes a change is not always the path
        # that evaluates: a rule's pre-eval collection sees the change, then the cron
        # tick's own scrape seconds later sees none — and without this flag that
        # second run would report force=False and leave other sleeping rules unwoken.
        self._changed_since_eval: dict[str, bool] = {}
        # background refreshes we fire ourselves (a target change); held so the event
        # loop can't garbage-collect a task mid-run
        self._background: set[asyncio.Task] = set()

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Discover every configured adapter, then schedule the enabled ones.
        Never auto-runs a source — scheduling only arms future cron ticks."""
        for source in self.ctx.config.sources:
            try:
                self._discover_one(source)
            except Exception as e:  # a broken adapter must not sink the others
                self._errors[source.key] = f"{type(e).__name__}: {e}"
                log.warning("source %s discovery failed: %s", source.key, e)

        for source in self.ctx.config.sources:
            if not (source.enabled and source.schedule and source.key in self._adapters):
                continue
            try:
                trigger = CronTrigger.from_crontab(source.schedule, timezone=self.ctx.settings.tz)
                self._sched.add_job(
                    self.run_source, trigger, args=[source.key],
                    kwargs={"scheduled": True},
                    id=f"source:{source.key}", name=f"source:{source.key}",
                    replace_existing=True, coalesce=True, max_instances=1,
                    misfire_grace_time=300,
                )
            except Exception as e:
                log.warning("source %s schedule '%s' invalid: %s", source.key, source.schedule, e)

        if not self._sched.running:
            self._sched.start()
        log.info("sources ready: %d discovered, %d scheduled",
                 len(self._adapters), len(self._sched.get_jobs()))

    async def stop(self) -> None:
        if self._sched.running:
            self._sched.shutdown(wait=False)

    # ── discovery ────────────────────────────────────────────────────────────
    def _discover_one(self, source: SourceConfig) -> SourceAdapter:
        """Import warden.adapters.<adapter>.adapter:Adapter and remember its fixtures dir."""
        mod = importlib.import_module(f"warden.adapters.{source.adapter}.adapter")
        adapter: SourceAdapter = getattr(mod, "Adapter")()
        self._adapters[source.key] = adapter
        self._fixtures[source.key] = Path(mod.__file__).resolve().parent / "fixtures"
        self._errors.pop(source.key, None)
        return adapter

    def _require_adapter(self, source: SourceConfig) -> SourceAdapter:
        adapter = self._adapters.get(source.key)
        return adapter if adapter is not None else self._discover_one(source)

    # ── context assembly ─────────────────────────────────────────────────────
    def _build_context(self, source: SourceConfig, live: Optional[bool]) -> SourceContext:
        secrets = resolve_secrets(source)
        # param overrides everything; else "auto": attempt live and let the adapter
        # degrade to fixtures if it can't authenticate (e.g. Atom uses a captured
        # Google session, not secrets — has_secrets is a poor gate). We only need a
        # runtime capable of a real fetch (httpx is always present; a browser, if the
        # adapter needs one, is gated inside the adapter).
        decided = live if live is not None else True
        return SourceContext(
            source_key=source.key,
            subject=source.subject,
            secrets=secrets,
            options=self._options_with_targets(source),
            since_cursor=self.ctx.db.get_kv(f"cursor:{source.key}"),
            fixtures_dir=str(self._fixtures.get(source.key, "")),
            live=decided,
        )

    # ── per-week targets (hub-owned, injected into the adapter's options) ────
    @staticmethod
    def week_start(today=None) -> str:
        from datetime import date, timedelta
        d = today or date.today()
        return str(d - timedelta(days=d.weekday()))          # Monday

    @staticmethod
    def target_key(source_key: str, subject: str, week: str) -> str:
        return f"target:{source_key}:{subject}:{week}"

    def published_target(self, source: SourceConfig,
                         week: Optional[str] = None) -> Optional[dict]:
        """The week's workload as the SOURCE itself published it, from its last state.

        Atom sets its own plan for each week and the adapter records it in the state of
        play it stores; reading it back here is what lets the hub and the UI use the real
        number without a second copy of the fetching logic. A snapshot from another week
        describes another week's plan, so it is ignored rather than shown as this one's.
        """
        week = week or self.week_start()
        raw = self.ctx.db.get_kv(f"state:{source.key}")
        if not raw:
            return None
        try:
            week_to_date = (json.loads(raw) or {}).get("week_to_date") or {}
        except Exception:
            return None
        if week_to_date.get("week_starts_monday") != week:
            return None
        plan = (week_to_date.get("target") or {}).get("published_by_atom")
        if not isinstance(plan, dict) or plan.get("total") is None:
            return None
        return plan

    def get_target(self, source: SourceConfig, week: Optional[str] = None) -> dict:
        """This week's workload target: a parent override if one is set, else the number
        the source published for the week, else the config default.

        How much a week should contain changes week to week, and Atom publishes its own
        plan for it (islands per subject, plus any mock test) — that plan is what the
        child is shown, so it is the default rather than a figure typed in once and left
        to rot. An override still wins for the week it was set in, and the config value
        is the last resort for a plan that could not be read. Keeping all of this out of
        the rule text means a rule can say "his weekly target" and stay correct when the
        number moves.
        """
        week = week or self.week_start()
        subject = source.subject or "default"
        default = source.options.get("weekly_island_target")
        published = self.published_target(source, week)
        raw = self.ctx.db.get_kv(self.target_key(source.key, subject, week))
        if raw:
            try:
                data = json.loads(raw)
                return {"week_start": week, "islands": int(data.get("islands")),
                        "origin": "override", "note": data.get("note", ""),
                        "set_at": data.get("set_at", ""), "published": published}
            except Exception:
                pass
        if published is not None:
            return {"week_start": week, "islands": int(published["total"]),
                    "origin": "atom-published", "note": published.get("note", ""),
                    "set_at": "", "published": published}
        return {"week_start": week,
                "islands": int(default) if default is not None else None,
                "origin": "config-default" if default is not None else "unset",
                "note": "", "set_at": "", "published": None}

    def set_target(self, source: SourceConfig, islands: int, week: Optional[str] = None,
                   note: str = "") -> dict:
        week = week or self.week_start()
        subject = source.subject or "default"
        payload = {"islands": int(islands), "note": note,
                   "set_at": utcnow().isoformat()}
        self.ctx.db.set_kv(self.target_key(source.key, subject, week),
                           json.dumps(payload))
        return {"week_start": week, "islands": int(islands), "origin": "override",
                **{k: payload[k] for k in ("note", "set_at")}}

    def clear_target(self, source: SourceConfig, week: Optional[str] = None) -> dict:
        """Drop this week's override so the source's own published plan takes over."""
        week = week or self.week_start()
        subject = source.subject or "default"
        self.ctx.db.del_kv(self.target_key(source.key, subject, week))
        return self.get_target(source, week)

    def refresh_after_target_change(self, key: str) -> None:
        """Re-collect and re-evaluate a source because its target moved.

        Nothing in the data changed, but the bar it is measured against did — and the
        daily share is derived from the weekly number, so "how much today?" changes the
        moment the parent saves 21 over 19. Without this the new target only bites at
        the next hourly poll, and a dynamic rule asleep until its own next check would
        judge tonight against last week's number. Marked as changed so the sweep is
        forced rather than skipped as "no new facts".
        """
        self._changed_since_eval[key] = True
        task = asyncio.create_task(self._run_quietly(key))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _run_quietly(self, key: str) -> None:
        try:
            await self.run_source(key)
        except Exception:
            log.exception("refresh after target change failed for %s", key)

    def _options_with_targets(self, source: SourceConfig) -> dict:
        """Adapters stay stateless, so the hub resolves the target and passes it in.

        The adapter re-reads the source's own plan live each run and prefers it, so what
        travels here matters most in two cases: an override, which must beat the plan,
        and a run where the plan could not be fetched, which then keeps the number from
        the last good run instead of falling back years.
        """
        opts = dict(source.options)
        target = self.get_target(source)
        if target.get("islands") is not None:
            opts["weekly_island_target"] = target["islands"]
            opts["weekly_island_target_origin"] = target["origin"]
        return opts

    # ── introspection for the API/UI ─────────────────────────────────────────
    def list_sources(self) -> list[dict]:
        out: list[dict] = []
        for source in self.ctx.config.sources:
            adapter = self._adapters.get(source.key)
            manifest: Optional[dict] = None
            if adapter is not None:
                manifest = adapter.manifest.model_dump()
                if source.subject:  # fill the <subject> placeholder for display
                    manifest["emits_signals"] = [
                        s.replace("<subject>", source.subject)
                        for s in manifest.get("emits_signals", [])
                    ]
            runs = self.ctx.db.list_runs(source.key, limit=1)
            job = self._sched.get_job(f"source:{source.key}") if self._sched.running else None
            next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
            # is the stored state real, or the offline sample?
            live_data, data_note = None, ""
            raw_state = self.ctx.db.get_kv(f"state:{source.key}")
            if raw_state:
                try:
                    dq = (json.loads(raw_state) or {}).get("data_quality") or {}
                    live_data, data_note = dq.get("live"), dq.get("note", "")
                except Exception:
                    pass
            out.append({
                "key": source.key,
                "live_data": live_data,
                "data_note": data_note,
                "display_name": source.display_name or source.key,
                "adapter": source.adapter,
                "enabled": source.enabled,
                "schedule": source.schedule,
                "subject": source.subject,
                "manifest": manifest,
                "error": self._errors.get(source.key),
                "last_run": runs[0].model_dump(mode="json") if runs else None,
                "next_run": next_run,
            })
        return out

    async def _read_documents(self, source: SourceConfig, items: list) -> list[dict]:
        """Hand any cached attachments to the hub's DocumentReader.

        Adapters only fetch files (mechanical, per the contract); interpreting them is
        the hub's job. Failures here must never fail the source run — a newsletter we
        couldn't read is a gap, not an outage.
        """
        if self.ctx.reader is None or not items:
            return []
        subj = self.ctx.config.subject(source.subject) if source.subject else None
        try:
            return await self.ctx.reader.process_items(
                items,
                subject_name=(subj.name if subj else (source.subject or "the child")),
                class_hint=self._class_hint(subj),
            )
        except Exception:
            log.exception("document reading for source %s failed", source.key)
            return []

    @staticmethod
    def _class_hint(subj) -> str:
        """The subject's class right now, from their date-driven contexts."""
        if subj is None or not getattr(subj, "contexts", None):
            return ""
        from datetime import date
        today = date.today().isoformat()
        for c in subj.contexts:
            frm, to = (c.effective_from or ""), (c.effective_to or "")
            if (not frm or frm <= today) and (not to or today <= to):
                return c.label
        return ""

    # ── freshness barrier (what the rule evaluator waits on) ─────────────────
    async def ensure_fresh(self, timeout: Optional[float] = None) -> list[str]:
        """Block until every enabled source's stored data is as current as its schedule.

        Two halves, both needed:
          * a collection already in flight is awaited — the hourly poll and a rule's own
            wake-up are both scheduled on the hour, so this is the common case;
          * a source whose scheduled poll has come round without a run to show for it is
            collected now, and awaited.
        Sources are done one at a time: they're scrapes, and two browser logins at once
        is a worse failure than a few seconds of extra wait.

        Bounded by `timeout` seconds per source (default WARDEN_COLLECT_WAIT_SEC): on
        expiry we evaluate against the data we already have and leave the collection
        running. Returns the source keys we waited on.
        """
        budget = float(timeout if timeout is not None else self.ctx.settings.collect_wait_sec)
        waited: list[str] = []
        for source in self.ctx.config.sources:
            if not (source.enabled and source.key in self._adapters):
                continue
            running = self._inflight.get(source.key)
            if (running is None or running.done()) and not self._collection_due(source):
                continue
            waited.append(source.key)
            try:
                await asyncio.wait_for(self.collect_now(source.key), timeout=budget)
            except asyncio.TimeoutError:
                log.warning("source %s still collecting after %.0fs — evaluating on the "
                            "data already stored", source.key, budget)
            except Exception:
                log.exception("pre-evaluation collection of %s failed", source.key)
        return waited

    def _collection_due(self, source: SourceConfig) -> bool:
        """Has this source's scheduled poll come round with no run to show for it?

        Compared against the last run ATTEMPTED, not the last one that succeeded: one
        attempt per scheduled tick means a source that is failing (expired session, site
        down) doesn't get hammered by every rule evaluation in the meantime. (The cron
        tick retries a failed attempt itself — see run_source's skip branch.)

        Known limitation, accepted: during the DST fall-back's repeated wall-clock hour
        (Europe/London, one hour a year) the previous cron fire computes an hour early,
        so hourly data can be judged fresh at two real hours old.
        """
        if not source.schedule:
            return False
        try:
            tz = ZoneInfo(self.ctx.settings.tz)
        except Exception:
            tz = timezone.utc
        try:
            due_at = croniter(source.schedule, datetime.now(tz) + _DUE_LEAD).get_prev(datetime)
        except Exception:  # a bad cron is the scheduler's problem to report, not ours
            return False
        runs = self.ctx.db.list_runs(source.key, limit=1)
        if not runs:
            return True
        started = runs[0].started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return started < due_at

    # ── the run pipeline ─────────────────────────────────────────────────────
    async def run_source(self, key: str, live: bool | None = None,
                         scheduled: bool = False) -> SourceRun:
        """A full cycle for one source: collect and persist, THEN evaluate the rules.

        `scheduled` marks the cron tick. A rule waking on the same instant collects
        via `ensure_fresh` first, so by the time the tick runs, this hour's data is
        often already stored — re-scraping it seconds later (as the audit showed,
        twin runs at :05 and :08 every hour) buys nothing and doubles the load on the
        source's session. The tick then skips straight to the evaluation sweep.
        Manual "Run now" keeps scheduled=False: an operator asking for a run gets one.
        """
        run: Optional[SourceRun] = None
        source = self.ctx.config.source(key)
        if scheduled and source is not None and not self._collection_due(source):
            # join any collection still in flight rather than reading a half-written
            # run — the change flag consumed below is only set once it finishes
            task = self._inflight.get(key)
            if task is not None and not task.done():
                run = await asyncio.shield(task)
            else:
                runs = self.ctx.db.list_runs(key, limit=1)
                last = runs[0] if runs else None
                # Only a SUCCESSFUL attempt covers the tick. _collection_due counts
                # failed attempts too — that is deliberate, it stops ensure_fresh
                # hammering a broken source between ticks — but the tick itself IS
                # the retry cadence, so a failure must not freeze the whole hour.
                if last is not None and last.ok:
                    run = last
            if run is not None:
                log.info("source %s already collected this tick — evaluating only", key)
        if run is None:
            run = await self.collect_now(key, live)
        if run.ok and self.ctx.evaluator is not None:
            # Tell the evaluator when the facts moved since the last sweep — whichever
            # path collected them — so a rule that is WAITING on that data sees it at
            # once (Luke finishing at 16:52 was collected at 17:00 and acted on at
            # 18:00). It is not a blanket force: a rule that has put itself to sleep
            # until tomorrow has already answered its question and stays asleep. See
            # RuleEvaluator.wakeable_early.
            #
            # Peek now, clear only after a sweep that really happened: evaluate_all
            # has refusal paths that RETURN rather than raise (stale source data, no
            # LLM backend), and a pop-then-restore-on-exception pattern would eat the
            # wake-up on those — and on a CancelledError, which `except Exception`
            # never sees. Known narrow limitation (accepted): a rule that evaluated
            # on pre-change data <60s before the sweep is deduped by the evaluator's
            # duplicate guard and catches up on its own next check.
            changed = self._changed_since_eval.get(key, False)
            try:
                results = await self.ctx.evaluator.evaluate_all(f"source:{key}",
                                                                on_data_change=changed)
            except Exception:
                log.exception("dynamic-rule eval after source %s failed", key)
            else:
                swept = not any(isinstance(r, dict) and r.get("skipped")
                                for r in (results or []))
                if swept:
                    self._changed_since_eval.pop(key, None)
        return run

    async def collect_now(self, key: str, live: bool | None = None) -> SourceRun:
        """Collect + persist only, joining a run already in flight.

        This is the half `ensure_fresh` waits on, so it must never call back into the
        evaluator — that would be a cycle (evaluate → wait for data → evaluate).
        """
        if self.ctx.config.source(key) is None:
            raise KeyError(f"unknown source: {key}")
        # shield: a caller that gives up waiting (its own timeout) must not abort a
        # scrape everyone else is waiting on.
        return await asyncio.shield(self._collection_task(key, live))

    def _collection_task(self, key: str, live: bool | None) -> asyncio.Task:
        task = self._inflight.get(key)
        if task is not None and not task.done():
            log.info("source %s already collecting — joining that run", key)
            return task
        task = asyncio.ensure_future(self._collect_and_store(key, live))
        self._inflight[key] = task
        task.add_done_callback(lambda t: self._inflight.pop(key, None)
                               if self._inflight.get(key) is t else None)
        return task

    async def _collect_and_store(self, key: str, live: bool | None = None) -> SourceRun:
        """authenticate → collect → persist → emit → audit. Any failure is captured
        into a failed SourceRun (never raised) so a scheduler tick can't crash the loop."""
        source = self.ctx.config.source(key)
        if source is None:
            raise KeyError(f"unknown source: {key}")

        started_at = utcnow()
        run_id = self.ctx.db.start_run(key)
        try:
            adapter = self._require_adapter(source)
            sctx = self._build_context(source, live)

            session = await adapter.authenticate(sctx)
            res = await adapter.collect(sctx, session)

            new_items = self.ctx.db.save_items(res.items)
            for sig in res.signals:
                await self.ctx.bus.emit(sig)          # drives the rules engine
                await self.ctx.emit_signal_update(sig.key)  # pushes value to the UI
            if res.next_cursor is not None:
                self.ctx.db.set_kv(f"cursor:{key}", res.next_cursor)
            # Read any newly-cached documents (the newsletter PDF) BEFORE storing the
            # state, so their digests land in the same "state of play" the dynamic rules
            # reason over rather than a cycle behind.
            docs = await self._read_documents(source, res.items)
            if docs and res.state is not None:
                res.state["documents"] = docs

            state_changed = False
            if res.state:      # latest "state of play" for the dynamic evaluator
                self.ctx.db.set_kv(f"state:{key}", json.dumps(res.state, default=str))
                # archive a snapshot when the data actually changed (progress over time)
                state_changed = self.ctx.db.record_state_snapshot(key, res.state)
                if state_changed:
                    self._changed_since_eval[key] = True
                    log.info("state changed for %s — snapshot archived", key)

            mode = "live" if sctx.live else "fixtures"
            detail = res.detail or (
                f"{mode}: {len(res.items)} items ({new_items} new), {len(res.signals)} signals"
            )
            self.ctx.db.finish_run(run_id, res.ok, len(res.items), len(res.signals), detail)
            await self.ctx.audit(f"source:{key}", "source_run", key, detail, ok=res.ok)
            await self.ctx.after_change("source")
            # Rule evaluation is deliberately NOT here — see run_source. Everything a
            # rule reads is persisted by the time this returns, which is the whole point.
            return SourceRun(
                id=run_id, source=key, started_at=started_at, finished_at=utcnow(),
                ok=res.ok, item_count=len(res.items), signal_count=len(res.signals),
                detail=detail, state_changed=state_changed,
            )
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            log.warning("source %s run failed: %s", key, detail)
            self.ctx.db.finish_run(run_id, False, 0, 0, detail)
            await self.ctx.audit(f"source:{key}", "source_run", key, detail, ok=False)
            return SourceRun(
                id=run_id, source=key, started_at=started_at, finished_at=utcnow(),
                ok=False, detail=detail,
            )
