"""User actions — the toggles/buttons that mutate AdGuard state.

Every endpoint here changes network state, so each one is wrapped exactly as the
contract requires: perform the mutation, then `ctx.audit(...)` and `ctx.after_change(...)`
so the change is logged and pushed live to every WS client. The AdGuardService stays
pure; auditing/notifying is the caller's job (that's us). actor is always "user".
"""
from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..models import AdGuardStatus, ServiceState, StateSnapshot

router = APIRouter()


# ── request bodies ───────────────────────────────────────────────────────────
class ServiceBody(BaseModel):
    group: str
    service: str
    state: ServiceState


class GroupBody(BaseModel):
    group: str
    state: ServiceState


class ClientBody(BaseModel):
    client: str
    state: ServiceState


class ProtectionBody(BaseModel):
    enabled: bool


class RuleBody(BaseModel):
    rule: str


# ── shared mutation wrapper ──────────────────────────────────────────────────
async def _mutate(ctx, action: str, target: str, detail: str,
                  op: Callable[[], Awaitable[None]]) -> None:
    """Run an AdGuard mutation, then audit + push a live update.

    On failure we still audit (ok=False) and refresh the snapshot before surfacing a
    502 to the caller, so the log and UI reflect reality.
    """
    try:
        await op()
    except Exception as e:  # let it be audited, then translate to HTTP
        await ctx.audit("user", action, target, f"failed: {e}", ok=False)
        await ctx.after_change("action")
        raise HTTPException(status_code=502, detail=str(e))
    await ctx.audit("user", action, target, detail)
    await ctx.after_change("action")


async def _adguard_rules_payload(ctx) -> dict:
    """The {rules, enabled} shape shared by GET/POST/DELETE of AdGuard custom rules."""
    rules = await ctx.adguard.list_rules()
    status = await ctx.adguard.status()
    return {"rules": rules, "enabled": bool(status.protection_enabled)}


# ── service / group / client / protection ────────────────────────────────────
@router.post("/actions/service")
async def action_service(body: ServiceBody, request: Request) -> StateSnapshot:
    ctx = request.app.state.ctx
    await _mutate(
        ctx, "set_service", f"{body.group}/{body.service}", body.state.value,
        lambda: ctx.adguard.set_service(body.group, body.service, body.state),
    )
    return await ctx.adguard.snapshot()


@router.post("/actions/group")
async def action_group(body: GroupBody, request: Request) -> StateSnapshot:
    ctx = request.app.state.ctx
    await _mutate(
        ctx, "set_group", body.group, body.state.value,
        lambda: ctx.adguard.set_group(body.group, body.state),
    )
    return await ctx.adguard.snapshot()


@router.post("/actions/client")
async def action_client(body: ClientBody, request: Request) -> StateSnapshot:
    ctx = request.app.state.ctx
    await _mutate(
        ctx, "set_client", body.client, body.state.value,
        lambda: ctx.adguard.set_client(body.client, body.state),
    )
    return await ctx.adguard.snapshot()


@router.post("/actions/protection")
async def action_protection(body: ProtectionBody, request: Request) -> AdGuardStatus:
    ctx = request.app.state.ctx
    await _mutate(
        ctx, "set_protection", "network",
        "enabled" if body.enabled else "disabled",
        lambda: ctx.adguard.set_protection(body.enabled),
    )
    return await ctx.adguard.status()


# ── AdGuard custom (user) filtering rules ────────────────────────────────────
@router.get("/adguard/rules")
async def get_adguard_rules(request: Request) -> dict:
    return await _adguard_rules_payload(request.app.state.ctx)


@router.post("/adguard/rules")
async def add_adguard_rule(body: RuleBody, request: Request) -> dict:
    ctx = request.app.state.ctx
    await _mutate(
        ctx, "add_rule", body.rule, "adguard custom rule",
        lambda: ctx.adguard.add_rule(body.rule),
    )
    return await _adguard_rules_payload(ctx)


@router.delete("/adguard/rules")
async def remove_adguard_rule(body: RuleBody, request: Request) -> dict:
    ctx = request.app.state.ctx
    await _mutate(
        ctx, "remove_rule", body.rule, "adguard custom rule",
        lambda: ctx.adguard.remove_rule(body.rule),
    )
    return await _adguard_rules_payload(ctx)
