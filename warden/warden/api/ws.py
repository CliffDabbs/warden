"""WebSocket endpoint — pushes live state/audit/signal updates to the SPA.

Mounted at /ws (no /api prefix). On connect we subscribe to the EventHub and send a
priming state snapshot + current signals, then forward every published event as JSON
until the client goes away. Producers (actions, engine, sources) call ctx.after_change /
ctx.audit / ctx.emit_signal_update, which publish onto the hub we're draining here.
"""
from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    ctx = websocket.app.state.ctx
    # gate the socket with the same cookie the HTTP middleware checks (http
    # middleware doesn't see websockets, so enforce it here)
    auth = getattr(ctx, "auth", None)
    if auth is not None and not auth.check_cookies(websocket.cookies):
        await websocket.close(code=1008)          # policy violation
        return
    await websocket.accept()
    q = ctx.events.subscribe()
    try:
        # ── prime the client with current state + signals ────────────────────
        try:
            snap = await ctx.adguard.snapshot()
            await websocket.send_json({"type": "state", "snapshot": snap.model_dump(mode="json")})
        except Exception as e:  # a broken snapshot must not abort the connection
            await websocket.send_json({"type": "error", "detail": f"snapshot failed: {e}"})
        await websocket.send_json({
            "type": "signals",
            "signals": [s.model_dump(mode="json") for s in ctx.bus.all()],
        })

        # ── forward events until the socket closes ───────────────────────────
        while True:
            event = await q.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass                       # normal client close
    except Exception:
        pass                       # send after close / dropped connection — clean up below
    finally:
        ctx.events.unsubscribe(q)
