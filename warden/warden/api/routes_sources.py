"""Sources + signals endpoints — run adapters, inspect items, drive the bus.

`/sources/*` project the registry (list/run) and the items each run persisted.
`/signals/*` read the bus, and `/signals/emit` lets the UI *inject* a signal —
emitting on the bus fans out to the engine's signal-trigger subscribers, which is
exactly how the demo "flip Luke's Atom-complete" fires the YouTube rule live.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
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


def _todays_share(ctx: Any, key: str, week_start: str) -> Optional[dict]:
    """Today's slice of the weekly target, read back from the source's stored state.

    The adapter already worked this out when it collected — it is the same number the
    daily_complete signal is judged against — so read it rather than keeping a second
    copy of the arithmetic here that could drift from it.
    """
    raw = ctx.db.get_kv(f"state:{key}")
    if not raw:
        return None
    try:
        state = json.loads(raw) or {}
    except Exception:
        return None
    week = state.get("week_to_date") or {}
    target = week.get("target") or {}
    req = target.get("todays_requirement") or {}
    # A snapshot from a different week describes a different target — say nothing
    # rather than show last week's share against this week's number.
    if not req or week.get("week_starts_monday") != week_start:
        return None
    return {
        "islands_due_today": req.get("islands_due_today"),
        "still_to_do_today": req.get("still_to_do_today"),
        "completed_today": target.get("completed_today"),
        "completed_week_to_date": target.get("completed_week_to_date",
                                             week.get("islands_completed")),
        "explain": req.get("explain") or req.get("basis"),
        "as_of": state.get("now"),
    }


class TargetBody(BaseModel):
    islands: int
    week_start: Optional[str] = None       # Monday, YYYY-MM-DD; default = this week
    note: str = ""


@router.get("/sources/{key}/target")
async def get_target(key: str, request: Request, week_start: Optional[str] = None) -> dict:
    """This week's workload target for a source (islands/week for Atom), plus the
    share of it that falls on today — the weekly number is what varies, so the daily
    one is derived from it rather than fixed.

    `origin` says where the number came from: "atom-published" (the source's own plan
    for the week, read live), "override" (set here by the parent, and it wins), or
    "config-default" (the fallback, used only when the plan could not be read).
    `published` carries the plan itself, so the UI can offer to go back to it."""
    ctx = request.app.state.ctx
    source = ctx.config.source(key)
    if source is None:
        raise HTTPException(status_code=404, detail=f"unknown source: {key}")
    res = ctx.registry.get_target(source, week_start)
    res["today"] = _todays_share(ctx, key, res["week_start"])
    return res


@router.put("/sources/{key}/target")
async def set_target(key: str, body: TargetBody, request: Request) -> dict:
    """Set this week's target — rules read it from the source's state rather than
    carrying the number in their text, so they stay correct when it changes.

    Takes effect immediately: the source is re-collected in the background, which
    recomputes today's share of the new total, republishes islands_due_today and
    daily_complete, and forces a rule sweep."""
    ctx = request.app.state.ctx
    source = ctx.config.source(key)
    if source is None:
        raise HTTPException(status_code=404, detail=f"unknown source: {key}")
    if body.islands < 0:
        raise HTTPException(status_code=400, detail="islands must be >= 0")
    res = ctx.registry.set_target(source, body.islands, body.week_start, body.note)
    await ctx.audit("user", "set_target", f"{key}:{res['week_start']}",
                    f"weekly target = {body.islands}"
                    + (f" ({body.note})" if body.note else ""))
    await ctx.after_change("target")
    ctx.registry.refresh_after_target_change(key)
    return res


@router.delete("/sources/{key}/target")
async def clear_target(key: str, request: Request,
                       week_start: Optional[str] = None) -> dict:
    """Drop this week's override and go back to the source's own published plan.

    Atom sets the week's workload itself and it changes every week, so an override is
    a deliberate exception ("he's ill", "half term") rather than the normal way to keep
    the number current — this is how you take the exception off again. Like setting one,
    it re-collects in the background so today's share is recomputed from the plan."""
    ctx = request.app.state.ctx
    source = ctx.config.source(key)
    if source is None:
        raise HTTPException(status_code=404, detail=f"unknown source: {key}")
    res = ctx.registry.clear_target(source, week_start)
    res["today"] = _todays_share(ctx, key, res["week_start"])
    await ctx.audit("user", "clear_target", f"{key}:{res['week_start']}",
                    f"back to {res['origin']}"
                    + (f" = {res['islands']}" if res.get("islands") is not None else ""))
    await ctx.after_change("target")
    ctx.registry.refresh_after_target_change(key)
    return res


@router.get("/sources/{key}/documents")
async def source_documents(key: str, request: Request) -> dict:
    """What was read out of this source's attached documents (the newsletter PDFs).

    Digests are cached by the file's content hash, so this is also the record of which
    newsletters have been read and what each one said about the child.
    """
    ctx = request.app.state.ctx
    reader = ctx.reader
    ok, why = (reader.available() if reader else (False, "no document reader"))
    raw = ctx.db.get_kv(f"state:{key}")
    docs: list = []
    if raw:
        try:
            docs = (json.loads(raw) or {}).get("documents") or []
        except Exception:
            docs = []
    return {"reader_available": ok, "reason": why, "count": len(docs), "documents": docs}


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
