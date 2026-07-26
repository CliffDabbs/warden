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
from typing import Any, Optional

from .config import Settings
from .db import DB
from .models import AuditEntry, WardenConfig, utcnow
from .signals.bus import SignalBus


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
        self.auth: Any = None           # Auth (login gate; None/disabled = open)

    # ── audit + change notification ──────────────────────────────────────────
    async def audit(self, actor: str, action: str, target: str = "",
                    detail: str = "", ok: bool = True) -> None:
        entry = AuditEntry(actor=actor, action=action, target=target, detail=detail, ok=ok)
        self.db.add_audit(entry)
        await self.events.publish({"type": "audit", "entry": entry.model_dump(mode="json")})

    async def after_change(self, reason: str = "") -> None:
        """Recompute the dashboard snapshot and push it to WS clients."""
        if self.adguard is None:
            return
        try:
            snap = await self.adguard.snapshot()
            await self.events.publish({
                "type": "state",
                "reason": reason,
                "snapshot": snap.model_dump(mode="json"),
            })
        except Exception as e:  # never let a UI push break an action
            await self.events.publish({"type": "error", "detail": f"snapshot failed: {e}"})

    async def emit_signal_update(self, key: str) -> None:
        sig = self.bus.get(key)
        if sig is not None:
            await self.events.publish({"type": "signal", "signal": sig.model_dump(mode="json")})
