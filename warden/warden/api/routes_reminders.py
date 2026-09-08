"""Daily reminders — the read-only "what does he need tomorrow?" projection.

Nothing here mutates AdGuard, so there's no audit/after_change on the GET. The refresh
POST is a cache bust rather than a state change: it re-reads the stored messages with the
LLM instead of reusing the cached reading, which is what you want after a source run has
brought in a message the page hasn't seen yet.

Built from data already collected by the Weduc adapter — this never scrapes. Use
`POST /api/sources/weduc/run` to fetch, then this to interpret.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/reminders")
async def get_reminders(request: Request) -> dict:
    """The whole Reminders page: school days, dated events, forms, parent actions."""
    ctx = request.app.state.ctx
    if ctx.reminders is None:
        raise HTTPException(503, "reminder builder not available")
    return await ctx.reminders.build()


@router.post("/reminders/refresh")
async def refresh_reminders(request: Request) -> dict:
    """Same payload, but re-read the messages rather than reusing the cached reading."""
    ctx = request.app.state.ctx
    if ctx.reminders is None:
        raise HTTPException(503, "reminder builder not available")
    return await ctx.reminders.build(force=True)
