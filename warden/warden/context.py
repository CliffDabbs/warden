"""AppContext — the wired-together services, shared via FastAPI app.state.ctx.

Also holds the EventHub: a tiny in-process broadcaster the WebSocket endpoint uses to
push live state/audit/signal updates to connected browsers. Any module that mutates
state (AdGuard service, rules engine, sources) should call ctx.after_change() so the
UI updates without polling.

Kept import-light on purpose: the heavy services (AdGuardService, RuleEngine,
SourceRegistry, RuleCompiler) are assigned as attributes in main.py, so this module
doesn't import them and there's no cycle.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .config import Settings
from .db import DB
from .models import AuditEntry, LlmCall, WardenConfig, utcnow
from .signals.bus import SignalBus

log = logging.getLogger("warden.context")


class EventHub:
    """Fan-out of JSON-able events to any number of subscriber queues (WS clients)."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)

    async def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow client; drop rather than block the producer


class AppContext:
    def __init__(self, settings: Settings, config: WardenConfig, db: DB, bus: SignalBus) -> None:
        self.settings = settings
        self.config = config
        self.db = db
        self.bus = bus
        self.events = EventHub()
        # assigned by main.py once built:
        self.adguard: Any = None        # AdGuardService
        self.compiler: Any = None       # RuleCompiler
        self.engine: Any = None         # RuleEngine
        self.evaluator: Any = None      # RuleEvaluator (dynamic LLM rules)
        self.registry: Any = None       # SourceRegistry
        self.reader: Any = None         # DocumentReader (newsletter PDFs)
        self.reminders: Any = None      # ReminderBuilder (the daily school reminders page)
        self.hosts: Any = None          # HostControl (start/stop services over SSH)
        self.auth: Any = None           # Auth (login gate; None/disabled = open)
        self.overrides: Any = None      # OverrideKeeper (expires timed device unblocks)

    # ── audit + change notification ──────────────────────────────────────────
    async def audit(self, actor: str, action: str, target: str = "",
                    detail: str = "", ok: bool = True, ref: str = "") -> None:
        entry = AuditEntry(actor=actor, action=action, target=target, detail=detail,
                           ok=ok, ref=ref)
        self.db.add_audit(entry)
        await self.events.publish({"type": "audit", "entry": entry.model_dump(mode="json")})

    async def record_llm(self, call: LlmCall) -> int:
        """Store an LLM exchange verbatim, and put a line in the activity feed for it.

        Every model call Warden makes comes through here — compiling a rule, evaluating
        a dynamic one, reading the newsletter. The feed is where the household looks to
        see what Warden did and why, and "the model decided" is only an answer if the
        exact question and the exact reply are one click from that line: `ref` carries
        the id, GET /api/llm/{id} serves the transcript.

        Never let the bookkeeping break the work it describes — the call has already
        happened by the time we get here, so a failure to record is logged, not raised.
        """
        try:
            call_id = self.db.add_llm_call(call)
        except Exception:
            log.exception("could not record the LLM call (%s)", call.purpose)
            return 0
        bits = [b for b in (call.model, call.backend) if b]
        if call.input_tokens or call.output_tokens:
            bits.append(f"{call.input_tokens or 0} in / {call.output_tokens or 0} out")
        bits.append(f"{call.ms / 1000:.1f}s")
        detail = " · ".join(bits) + (f" — {call.error}" if call.error else "")
        await self.audit("llm", "llm_call", call.purpose or "llm", detail,
                         ok=call.ok, ref=f"llm:{call_id}")
        return call_id

    async def after_change(self, reason: str = "") -> None:
        """Recompute the dashboard snapshot and push it to WS clients."""
        if self.adguard is None:
            return
        try:
            snap = await self.adguard.snapshot()
            holds = {f"{g}/{svc}": {"state": row["state"],
                                    "since": row.get("created_at", ""),
                                    "expires_at": row.get("expires_at", "")}
                     for (g, svc), row in self.db.active_pins(utcnow().isoformat()).items()}
            await self.events.publish({
                "type": "state",
                "reason": reason,
                "snapshot": snap.model_dump(mode="json"),
                "holds": holds,
            })
        except Exception as e:  # never let a UI push break an action
            await self.events.publish({"type": "error", "detail": f"snapshot failed: {e}"})

    async def emit_signal_update(self, key: str) -> None:
        sig = self.bus.get(key)
        if sig is not None:
            await self.events.publish({"type": "signal", "signal": sig.model_dump(mode="json")})
