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
async def get_state(request: Request) -> StateSnapshot:
    """Full dashboard snapshot: AdGuard status + every managed group's toggles."""
    return await request.app.state.ctx.adguard.snapshot()


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
    }


@router.get("/audit")
async def get_audit(request: Request, limit: int = 100) -> list[AuditEntry]:
    """Most-recent audit entries (newest first)."""
    return request.app.state.ctx.db.list_audit(limit)
