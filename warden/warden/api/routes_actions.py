"""User actions — the toggles/buttons that mutate AdGuard state.

Every endpoint here changes network state, so each one is wrapped exactly as the
contract requires: perform the mutation, then `ctx.audit(...)` and `ctx.after_change(...)`
so the change is logged and pushed live to every WS client. The AdGuardService stays
pure; auditing/notifying is the caller's job (that's us). actor is always "user".
"""
from __future__ import annotations

import ipaddress
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from typing import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..models import AdGuardStatus, DeviceOverride, ServiceState, StateSnapshot, utcnow

log = logging.getLogger("warden.actions")
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
    # Required when disabling (see action_protection): who is doing this and why.
    # Turning protection off silences EVERY control in the house at once, and the
    # audit trail used to show only "user … disabled" — nothing to say who or why.
    reason: str = ""
    device: str = ""          # self-declared device name, remembered by the browser


class RuleBody(BaseModel):
    rule: str


# ── manual holds ─────────────────────────────────────────────────────────────
def _pin_expiry(ctx) -> str:
    """Fallback expiry for a manual hold: the next 04:00 local.

    The REAL reset is a scheduled rule writing the switch (the 8pm switchoff clears
    the day's holds); this timestamp only stops a hold outliving everything when no
    schedule ever touches that switch.
    """
    try:
        tz = ZoneInfo(ctx.settings.tz)
    except Exception:
        tz = timezone.utc
    now = datetime.now(tz)
    four = now.replace(hour=4, minute=0, second=0, microsecond=0)
    if four <= now:
        four += timedelta(days=1)
    return four.astimezone(timezone.utc).isoformat()


def _pin_flips(ctx, flips: list, actor: str = "user") -> None:
    """Record manual switch flips as holds the dynamic evaluator must not undo."""
    expires = _pin_expiry(ctx)
    for group, service, state in flips:
        ctx.db.pin_switch(group, service,
                          getattr(state, "value", str(state)), actor, expires)


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


# ── host services (start/stop things DNS can't reach) ────────────────────────
class HostServiceBody(BaseModel):
    running: bool


@router.get("/hosts/services")
async def list_host_services(request: Request) -> list[dict]:
    """Managed services and their live state. Probing each opens an SSH connection, so
    this is a little slower than the other reads."""
    ctx = request.app.state.ctx
    if ctx.hosts is None:
        return []
    return await ctx.hosts.list_services()


@router.post("/hosts/services/{sid}")
async def set_host_service(sid: str, body: HostServiceBody, request: Request) -> dict:
    ctx = request.app.state.ctx
    svc = ctx.config.managed_service(sid)
    if svc is None:
        raise HTTPException(status_code=404, detail=f"no such managed service: {sid}")
    try:
        res = await ctx.hosts.set_running(svc, body.running)
    except Exception as e:
        await ctx.audit("user", "host_service", sid, f"failed: {e}", ok=False)
        raise HTTPException(status_code=502, detail=str(e))
    # a manual start/stop of a whole-house service is a hold like any switch flip:
    # the dynamic evaluator must not flip it back (movie night stays a movie night)
    ctx.db.pin_switch("_host", sid, "running" if body.running else "stopped",
                      "user", _pin_expiry(ctx))
    await ctx.after_change("host_service")
    return res


@router.get("/hosts/{name}/discover")
async def discover_host(name: str, request: Request) -> dict:
    """What does this box offer — qpkg_cli, docker, systemd, init.d?

    A QNAP may run Plex as a QPKG or as a Container Station container, and the correct
    start/stop command differs. Rather than guess in config, look.
    """
    ctx = request.app.state.ctx
    host = ctx.config.host(name)
    if host is None:
        raise HTTPException(status_code=404, detail=f"no such host: {name}")
    ok, why = ctx.hosts.available(host)
    if not ok:
        raise HTTPException(status_code=400, detail=why)
    return await ctx.hosts.discover(host)


# ── quick actions (configurable one-tap presets) ─────────────────────────────
def _expand(ctx, action) -> list[tuple[str, str, ServiceState]]:
    """Resolve a quick action's steps into concrete (group, service, state) flips.

    A step naming only a `service` fans out to every group that exposes it — that's
    what "everywhere" means — while a step naming a `group` covers that group's whole
    managed service list. Unknown ids are skipped rather than failing the action.
    """
    out: list[tuple[str, str, ServiceState]] = []
    for step in action.steps:
        if step.service:
            svc = ctx.config.service(step.service)
            if svc is None:
                continue
            targets = step.groups or step.group and [step.group] or svc.groups
            for g in targets:
                if ctx.config.group(g) and step.service in [
                        s.id for s in ctx.config.services_for_group(g)]:
                    out.append((g, step.service, step.state))
        elif step.group:
            if ctx.config.group(step.group) is None:
                continue
            for svc in ctx.config.services_for_group(step.group):
                out.append((step.group, svc.id, step.state))
    # de-duplicate, last write wins
    seen: dict[tuple[str, str], ServiceState] = {}
    for g, s, st in out:
        seen[(g, s)] = st
    return [(g, s, st) for (g, s), st in seen.items()]


@router.get("/actions/quick")
async def list_quick_actions(request: Request) -> list[dict]:
    ctx = request.app.state.ctx
    return [
        {**q.model_dump(mode="json"), "affects": [
            {"group": g, "service": s, "state": st.value} for g, s, st in _expand(ctx, q)]}
        for q in ctx.config.quick_actions
    ]


@router.post("/actions/quick/{aid}")
async def run_quick_action(aid: str, request: Request) -> StateSnapshot:
    ctx = request.app.state.ctx
    action = ctx.config.quick_action(aid)
    if action is None:
        raise HTTPException(status_code=404, detail=f"no such quick action: {aid}")

    flips = _expand(ctx, action)
    if not flips:
        raise HTTPException(status_code=400,
                            detail=f"quick action '{aid}' resolves to no valid switches")

    # Snapshot only the switches we're about to touch, so a timed revert restores
    # exactly what was there rather than imposing a blanket baseline.
    prior: list[tuple[str, str, ServiceState]] = []
    if action.revert_after_minutes:
        snap = await ctx.adguard.snapshot()
        current = {(g.name, t.id): t.state for g in snap.groups for t in g.services}
        prior = [(g, s, current[(g, s)]) for g, s, _ in flips if (g, s) in current]

    applied: list[str] = []
    pinned: list = []
    for group, service, state in flips:
        try:
            await ctx.adguard.set_service(group, service, state)
            applied.append(f"{group}/{service}={state.value}")
            pinned.append((group, service, state))
        except Exception as e:
            await ctx.audit("user", "quick_action", f"{aid}:{group}/{service}",
                            f"failed: {e}", ok=False)

    # host-service steps (start/stop a real service — global, so never auto-reverted)
    for step in action.steps:
        if not step.host_service or step.running is None:
            continue
        svc = ctx.config.managed_service(step.host_service)
        if svc is None or ctx.hosts is None:
            continue
        try:
            await ctx.hosts.set_running(svc, step.running)
            applied.append(f"{svc.id}={'running' if step.running else 'stopped'}")
            ctx.db.pin_switch("_host", svc.id,
                              "running" if step.running else "stopped",
                              "user", _pin_expiry(ctx))
        except Exception as e:
            await ctx.audit("user", "quick_action", f"{aid}:{svc.id}",
                            f"failed: {e}", ok=False)

    detail = f"{action.label}: {', '.join(applied) if applied else 'nothing changed'}"
    if action.revert_after_minutes and prior:
        ctx.engine.arm_quick_revert(aid, action.label, prior, action.revert_after_minutes)
        detail += f" (reverts in {action.revert_after_minutes}m)"
    # a quick action is a manual choice: hold its switches against the evaluator
    # (its own timed revert clears the hold when it restores things)
    _pin_flips(ctx, pinned)
    await ctx.audit("user", "quick_action", aid, detail)
    await ctx.after_change("quick_action")
    return await ctx.adguard.snapshot()


# ── service / group / client / protection ────────────────────────────────────
@router.post("/actions/service")
async def action_service(body: ServiceBody, request: Request) -> StateSnapshot:
    ctx = request.app.state.ctx
    # pin FIRST: the WS push inside _mutate carries holds, so the badge appears
    # with the flip; a failed write unpins to avoid holding a switch that never moved
    _pin_flips(ctx, [(body.group, body.service, body.state)])
    try:
        await _mutate(
            ctx, "set_service", f"{body.group}/{body.service}",
            f"{body.state.value} · held until the next scheduled reset",
            lambda: ctx.adguard.set_service(body.group, body.service, body.state),
        )
    except Exception:
        ctx.db.clear_pins([(body.group, body.service)])
        raise
    return await ctx.adguard.snapshot()


@router.post("/actions/group")
async def action_group(body: GroupBody, request: Request) -> StateSnapshot:
    ctx = request.app.state.ctx
    flips = [(body.group, svc.id, body.state)
             for svc in ctx.config.services_for_group(body.group)]
    _pin_flips(ctx, flips)
    try:
        await _mutate(
            ctx, "set_group", body.group,
            f"{body.state.value} · held until the next scheduled reset",
            lambda: ctx.adguard.set_group(body.group, body.state),
        )
    except Exception:
        ctx.db.clear_pins([(g, svc) for g, svc, _ in flips])
        raise
    return await ctx.adguard.snapshot()


@router.post("/actions/client")
async def action_client(body: ClientBody, request: Request) -> StateSnapshot:
    ctx = request.app.state.ctx
    # A manual per-device flip is a hold like any other. Pins have no device
    # dimension, so an unblock records the DeviceOverride the group writer already
    # honours (before the write — same reasoning as routes_devices.unblock_device);
    # a manual block withdraws any standing exemption.
    if body.state is ServiceState.allowed:
        ctx.db.upsert_override(DeviceOverride(
            client=body.client, kind="unblock",
            created_at=utcnow().isoformat(), expires_at=_pin_expiry(ctx), actor="user"))
    else:
        ctx.db.delete_override(body.client)
    try:
        await _mutate(
            ctx, "set_client", body.client, body.state.value,
            lambda: ctx.adguard.set_client(body.client, body.state),
        )
    except Exception:
        if body.state is ServiceState.allowed:
            ctx.db.delete_override(body.client)   # no phantom exemption
        raise
    return await ctx.adguard.snapshot()


# Reasons that are really "the kids want to stream" — the one justification that is
# never valid for a whole-house protection kill, because per-service switches exist
# for exactly that. Word-boundary match, case-insensitive. Named services include the
# ones AdGuard blocks permanently (tiktok, roblox) and the big streamers Warden does
# not manage — "twitch isn't loading" is still a streaming excuse.
_STREAMING_EXCUSE = re.compile(
    r"\b(kids?|children|child|luke|asta|stream(?:ing)?|watch(?:ing)?|you\s?tube|yt|"
    r"netflix|disney(?:\+|plus)?|plex|xbox|film|movie|cartoons?|tv|twitch|tik\s?tok|"
    r"roblox|minecraft|fortnite|prime(?:\s?video)?|iplayer|hulu|crunchyroll|"
    r"paramount|sky)\b", re.IGNORECASE)

_MIN_REASON = 8    # characters, after trimming — "wifi bad" length; blocks "asdf"

# The docker bridge: LAN requests reach the container from the gateway, so an address
# in this range identifies nobody. (The full RFC1918 172.16/12, not startswith("172.")
# — 172.5.x is a public address and must not be swallowed by this check.)
_DOCKER_NET = ipaddress.ip_network("172.16.0.0/12")


def _clean_fragment(s: str, limit: int) -> str:
    """A user-supplied string, made safe to embed in the audit line.

    The audit detail's grammar is `DISABLED by <who> — reason: "<reason>"`, and both
    halves are read by humans deciding who to believe — so strip the characters that
    let one half impersonate the other's framing (a device named `Dad PC — reason:
    "adguard broke` would otherwise display a forged reason before the real one).
    """
    s = re.sub(r'[\"“”—]', " ", s)       # double quotes + em-dash
    s = re.sub(r"reason\s*:", " ", s, flags=re.IGNORECASE)
    return " ".join(s.split())[:limit].strip()


def _requester(request: Request, declared_device: str) -> str:
    """Best identification we can manage for who is making this request.

    Self-declared device name first (required to disable; the browser remembers it),
    corroborated by the socket peer address when it says anything — resolved to a
    config client name when it matches one. Deliberately NOT X-Forwarded-For: there
    is no proxy in this deployment, so that header is just a free impersonation slot
    (anyone could stamp their sibling's iPad onto their own request). The socket
    address is the one signal the requester cannot choose. Behind docker bridge
    networking it is the gateway (172.x) and says nothing; native runs see real IPs.
    """
    ctx = request.app.state.ctx
    parts: list[str] = []
    dev = _clean_fragment(declared_device, 60)
    if dev:
        parts.append(dev)
    raw_ip = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(raw_ip)
    except ValueError:
        ip = None
    if ip is not None and not (ip.is_loopback or ip in _DOCKER_NET):
        known = next((c.name for c in ctx.config.clients if raw_ip in (c.ids or [])), None)
        parts.append(f"{known} ({raw_ip})" if known else raw_ip)
    elif not parts:
        parts.append(f"unidentified device via {raw_ip or 'unknown'}")
    ua = (request.headers.get("user-agent") or "")
    for tag, label in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                       ("Windows", "Windows"), ("Macintosh", "Mac")):
        if tag in ua:
            parts.append(label)
            break
    return ", ".join(parts)


async def _refuse(ctx, who: str, reason: str, why: str, message: str) -> None:
    """Refuse a disable attempt — and leave a trace.

    A refusal is exactly the moment accountability matters: someone iterating
    wordings until one slips past the filter should be visible in the feed, not
    silently 422'd until they succeed with a laundered reason.
    """
    await ctx.audit("user", "protection_refused", "network",
                    f"refused ({why}) — {who} offered: \"{reason or 'nothing'}\"", ok=False)
    raise HTTPException(status_code=422, detail=message)


@router.post("/actions/protection")
async def action_protection(body: ProtectionBody, request: Request) -> AdGuardStatus:
    ctx = request.app.state.ctx
    who = _requester(request, body.device)

    if not body.enabled:
        # Disabling is the nuclear switch: every block Warden holds goes inert for
        # everyone. It stays possible — sometimes filtering genuinely breaks something
        # ("my work isn't working") — but never anonymously and never "for the kids".
        reason = _clean_fragment(body.reason, 200)
        if len(reason) < _MIN_REASON:
            await _refuse(ctx, who, reason, "no usable reason", (
                "Turning ALL protection off needs a real reason (a few words, e.g. "
                "\"my work VPN isn't connecting\"). It is recorded in the activity log."))
        m = _STREAMING_EXCUSE.search(reason)
        if m:
            await _refuse(ctx, who, reason, f"streaming excuse: '{m.group(0)}'", (
                f"That reads like a streaming unblock (\"{m.group(0)}\") — protection "
                "off is the wrong tool for that. Use the Streaming buttons on the "
                "Dashboard: they open just those services, instead of switching off "
                "every protection for everyone in the house. If your reason is "
                "genuinely something else, reword it without that word."))
        if not _clean_fragment(body.device, 60):
            await _refuse(ctx, who, reason, "anonymous", (
                "Say who is turning it off — give this device a name (it's remembered, "
                "so you'll only be asked once)."))
        detail = f"DISABLED by {who} — reason: \"{reason}\""
        log.warning("protection disabled by %s — reason: %s", who, reason)
    else:
        detail = f"enabled by {who}"

    await _mutate(
        ctx, "set_protection", "network", detail,
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
