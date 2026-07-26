"""SourceRegistry — discovers pluggable adapters and drives their runs.

This is the "hub" side of the source-adapter contract (docs/sourceadapterCONTRACT.md
§4): it knows nothing source-specific. For each `config.sources` entry it imports the
named adapter package, assembles a `SourceContext` per run, and orchestrates the
mechanical pipeline:

    start_run → authenticate → collect → save_items → emit signals → store cursor
             → finish_run → audit → after_change

Signals are emitted onto the bus (which drives the rules engine) and pushed to the UI.
Enabled sources are scheduled on their cron cadence, but nothing runs on boot — a run
is always an explicit schedule tick or an operator/API "run now".
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import logging
from pathlib import Path
from typing import Any, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..config import resolve_secrets
from ..context import AppContext
from ..models import SourceConfig, SourceRun, utcnow
from .base import SourceAdapter, SourceContext

log = logging.getLogger("warden.sources")


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
            options=source.options,
            since_cursor=self.ctx.db.get_kv(f"cursor:{source.key}"),
            fixtures_dir=str(self._fixtures.get(source.key, "")),
            live=decided,
        )

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
            out.append({
                "key": source.key,
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

    # ── the run pipeline ─────────────────────────────────────────────────────
    async def run_source(self, key: str, live: bool | None = None) -> SourceRun:
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
            if res.state:      # latest "state of play" for the dynamic evaluator
                self.ctx.db.set_kv(f"state:{key}", json.dumps(res.state, default=str))
                # archive a snapshot when the data actually changed (progress over time)
                if self.ctx.db.record_state_snapshot(key, res.state):
                    log.info("state changed for %s — snapshot archived", key)

            mode = "live" if sctx.live else "fixtures"
            detail = res.detail or (
                f"{mode}: {len(res.items)} items ({new_items} new), {len(res.signals)} signals"
            )
            self.ctx.db.finish_run(run_id, res.ok, len(res.items), len(res.signals), detail)
            await self.ctx.audit(f"source:{key}", "source_run", key, detail, ok=res.ok)
            await self.ctx.after_change("source")
            # fresh data may now satisfy a dynamic (LLM-evaluated) rule — re-evaluate
            if res.ok and self.ctx.evaluator is not None:
                try:
                    await self.ctx.evaluator.evaluate_all(f"source:{key}")
                except Exception:
                    log.exception("dynamic-rule eval after source %s failed", key)
            return SourceRun(
                id=run_id, source=key, started_at=started_at, finished_at=utcnow(),
                ok=res.ok, item_count=len(res.items), signal_count=len(res.signals),
                detail=detail,
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
