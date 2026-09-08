"""Read-only state endpoints — the dashboard's initial + on-demand data.

Nothing here mutates AdGuard, so there's no audit/after_change; these just project
the live snapshot, status, static config vocabulary and the audit log for the SPA.
All served under the /api prefix (added in main.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..models import AdGuardStatus, AuditEntry, StateSnapshot

router = APIRouter()


@router.get("/state")
async def get_state(request: Request) -> dict:
    """Full dashboard snapshot: AdGuard status + every managed group's toggles.

    `holds` rides alongside (not inside) the snapshot: manual flips the dynamic
    evaluator must not undo, keyed "group/service", so the UI can badge them.
    """
    ctx = request.app.state.ctx
    snap = await ctx.adguard.snapshot()
    return {**snap.model_dump(mode="json"), "holds": _holds(ctx)}


def _holds(ctx) -> dict:
    from ..models import utcnow
    return {f"{g}/{svc}": {"state": row["state"], "since": row.get("created_at", ""),
                           "expires_at": row.get("expires_at", "")}
            for (g, svc), row in ctx.db.active_pins(utcnow().isoformat()).items()}


@router.get("/status")
async def get_status(request: Request) -> AdGuardStatus:
    """Just the AdGuard connection/protection status pill."""
    return await request.app.state.ctx.adguard.status()


@router.get("/config")
async def get_config(request: Request) -> dict:
    """Static vocabulary the UI renders controls from (groups/services/subjects/…).

    Everything comes from config/warden.yaml — the SPA needs it to know which toggles
    to draw per group and which signals it can simulate.
    """
    ctx = request.app.state.ctx
    cfg = ctx.config
    return {
        "adguard": {"url": cfg.adguard.url},
        "compiler": {
            "backend": ctx.compiler.backend_name(),      # anthropic-api | claude-cli | builtin-parser
            "model": ctx.settings.llm_model,
            # the dynamic evaluator may run on a stronger model than the compiler
            "eval_model": ctx.settings.eval_model or ctx.settings.llm_model,
        },
        "auth": {"enabled": bool(ctx.auth and ctx.auth.enabled)},
        "groups": [
            {
                "name": g.name,
                "tag": g.tag,
                # the services that apply to this group, in config order
                "services": [s.model_dump() for s in cfg.services_for_group(g.name)],
            }
            for g in cfg.groups
        ],
        "services": [s.model_dump() for s in cfg.services],
        "subjects": [s.model_dump() for s in cfg.subjects],
        "sources": [
            {
                "key": s.key,
                "display_name": s.display_name,
                "enabled": s.enabled,
                "schedule": s.schedule,
                "subject": s.subject,
            }
            for s in cfg.sources
        ],
        "signals": [s.model_dump(mode="json") for s in cfg.signals],
        # one-tap Dashboard presets; the UI builds its buttons from this
        "quick_actions": [q.model_dump(mode="json") for q in cfg.quick_actions],
    }


@router.get("/audit")
async def get_audit(request: Request, limit: int = 100) -> list[AuditEntry]:
    """Most-recent audit entries (newest first)."""
    return request.app.state.ctx.db.list_audit(limit)
