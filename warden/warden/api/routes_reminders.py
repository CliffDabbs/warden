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
    """The whole Reminders page: school days, dated events, forms, parent actions.

    Never waits on the model. Reading the school messages takes 15-20 seconds, so if
    there is no reading cached for the messages currently in hand, the page comes back
    at once with `llm.pending` — everything that doesn't need a model is already on it —
    and the reading runs in the background. A `reminders` event over the WebSocket says
    when it has landed, and the next GET serves it from cache.
    """
    ctx = request.app.state.ctx
    if ctx.reminders is None:
        raise HTTPException(503, "reminder builder not available")
    return await ctx.reminders.build(wait=False)


@router.post("/reminders/refresh")
async def refresh_reminders(request: Request) -> dict:
    """Same payload, but re-read the messages rather than reusing the cached reading.

    This one DOES wait: it is a button press with its own "Re-reading…" state, so the
    answer is what was asked for.
    """
    ctx = request.app.state.ctx
    if ctx.reminders is None:
        raise HTTPException(503, "reminder builder not available")
    return await ctx.reminders.build(force=True)
