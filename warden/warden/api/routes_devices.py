"""Device overrides — per-device exemptions from Warden's group blocks.

The dashboard thinks in groups; this thinks in devices. It lists every client AdGuard
knows about (managed or not) and lets one be unblocked for a fixed window, or until
cancelled. While an override stands, AdGuardService._apply skips that device, so a
scheduled rule firing at 19:00 can't quietly undo a parent's manual unblock.

Same contract as routes_actions: mutate, then audit + push, with actor "user".
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..models import DeviceOverride, ServiceState, utcnow

router = APIRouter()

# What the UI offers. None = "forever" (no expiry; stands until cancelled).
DURATIONS: dict[str, Optional[int]] = {"2h": 2, "4h": 4, "24h": 24, "forever": None}


class UnblockBody(BaseModel):
    client: str
    duration: str = "2h"          # one of DURATIONS


def _overrides_map(ctx) -> dict[str, DeviceOverride]:
    return {o.client: o for o in ctx.db.list_overrides()}


@router.get("/devices")
async def list_devices(request: Request) -> dict:
    ctx = request.app.state.ctx
    devices = await ctx.adguard.list_devices(_overrides_map(ctx))
    return {
        "devices": [d.model_dump(mode="json") for d in devices],
        "durations": list(DURATIONS.keys()),
    }


@router.post("/devices/unblock")
async def unblock_device(body: UnblockBody, request: Request) -> dict:
    ctx = request.app.state.ctx
    if body.duration not in DURATIONS:
        raise HTTPException(400, f"unknown duration: {body.duration!r}")

    hours = DURATIONS[body.duration]
    now = utcnow()
    expires = (now + timedelta(hours=hours)).isoformat() if hours else None
    label = f"{hours}h" if hours else "until cancelled"

    # Record the exemption BEFORE unblocking, so a rule firing in the gap between the
    # two can't re-block the device a moment after we cleared it.
    ctx.db.upsert_override(DeviceOverride(
        client=body.client, kind="unblock",
        created_at=now.isoformat(), expires_at=expires, actor="user",
    ))
    try:
        await ctx.adguard.set_device_services(body.client, ServiceState.allowed)
    except Exception as e:
        ctx.db.delete_override(body.client)          # don't leave a phantom exemption
        await ctx.audit("user", "device.unblock", body.client, f"failed: {e}", ok=False)
        await ctx.after_change("device.unblock")
        raise HTTPException(502, str(e))

    await ctx.audit("user", "device.unblock", body.client, f"unblocked for {label}")
    await ctx.after_change("device.unblock")
    return {"ok": True, "client": body.client, "expires_at": expires}


@router.delete("/devices/override")
async def cancel_override(body: UnblockBody, request: Request) -> dict:
    """Cancel an override early and put the device's blocks straight back."""
    ctx = request.app.state.ctx
    if ctx.db.get_override(body.client) is None:
        raise HTTPException(404, f"no override for {body.client!r}")
    ctx.db.delete_override(body.client)
    try:
        await ctx.adguard.set_device_services(body.client, ServiceState.blocked)
    except Exception as e:
        await ctx.audit("user", "device.reblock", body.client, f"failed: {e}", ok=False)
        await ctx.after_change("device.reblock")
        raise HTTPException(502, str(e))
    await ctx.audit("user", "device.reblock", body.client, "override cancelled — re-blocked")
    await ctx.after_change("device.reblock")
    return {"ok": True, "client": body.client}
