"""Sources + signals endpoints — run adapters, inspect items, drive the bus.

`/sources/*` project the registry (list/run) and the items each run persisted.
`/signals/*` read the bus, and `/signals/emit` lets the UI *inject* a signal —
emitting on the bus fans out to the engine's signal-trigger subscribers, which is
exactly how the demo "flip Luke's Atom-complete" fires the YouTube rule live.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..models import Item, Signal, SignalType, SourceRun

router = APIRouter()


class EmitBody(BaseModel):
    key: str
    value: Any


# ── sources ──────────────────────────────────────────────────────────────────
@router.get("/sources")
async def list_sources(request: Request) -> list[dict]:
    return request.app.state.ctx.registry.list_sources()


@router.post("/sources/{key}/run")
async def run_source(key: str, request: Request, live: Optional[bool] = None) -> SourceRun:
    """Run one source now. `live` overrides fixtures-vs-real; default (None) lets the
    registry decide (fixtures unless secrets + a browser are available)."""
    return await request.app.state.ctx.registry.run_source(key, live)


@router.get("/sources/{key}/items")
async def source_items(key: str, request: Request, limit: int = 50) -> list[Item]:
    return request.app.state.ctx.db.recent_items(key, limit)


@router.get("/sources/{key}/history")
async def source_history(key: str, request: Request, limit: int = 200) -> list[dict]:
    """Archived 'state of play' snapshots (newest first) — one per change, for
    analysing progress over time (e.g. minutes/day, topics completed, assignments)."""
    return request.app.state.ctx.db.state_history(key, limit)


# ── signals ──────────────────────────────────────────────────────────────────
@router.get("/signals")
async def list_signals(request: Request) -> list[Signal]:
    return request.app.state.ctx.bus.all()


@router.get("/signals/{key}/history")
async def signal_history(key: str, request: Request, limit: int = 50) -> list[Signal]:
    return request.app.state.ctx.bus.history(key, limit)


@router.post("/signals/emit")
async def emit_signal(body: EmitBody, request: Request) -> Signal:
    """Inject a signal onto the bus (drives the engine) and push a live UI update.

    The declared type in config coerces the incoming JSON value so a bool signal
    becomes a real bool — essential for the bus's edge detection.
    """
    ctx = request.app.state.ctx
    sdef = next((s for s in ctx.config.signals if s.key == body.key), None)
    stype = sdef.type if sdef else SignalType.bool

    value: Any = body.value
    if stype == SignalType.bool:
        value = (value.strip().lower() in ("1", "true", "yes", "on")
                 if isinstance(value, str) else bool(value))
    elif stype == SignalType.number:
        value = float(value)
    else:
        value = str(value)

    sig = Signal(
        key=body.key, value=value, type=stype,
        source=sdef.source if sdef else None,
        subject=sdef.subject if sdef else None,
    )
    await ctx.bus.emit(sig)               # fan out to the engine's subscribers
    await ctx.emit_signal_update(body.key)  # push the new value to WS clients
    return sig
