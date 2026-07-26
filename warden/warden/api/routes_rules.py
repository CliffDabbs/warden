"""Rule CRUD — the plain-English rules the engine schedules/fires.

Preview compiles without persisting (400 on a genuine parse failure). Create/update
store the compiled AST (or a compile_error) and — crucially — call
`ctx.engine.reload_rules()` after every create/update/toggle/delete so the running
engine's schedule + signal subscriptions stay in sync with the store.
"""
from __future__ import annotations

from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..rules.schema import CompiledRule, Rule

router = APIRouter()


# ── request bodies ───────────────────────────────────────────────────────────
class PreviewBody(BaseModel):
    text: str


class CreateBody(BaseModel):
    text: str
    enabled: bool = True


class UpdateBody(BaseModel):
    text: Optional[str] = None
    enabled: Optional[bool] = None


class RunBody(BaseModel):
    dry: bool = False


async def _compile(ctx, text: str) -> tuple[Optional[CompiledRule], Optional[str]]:
    """Compile, capturing failure as a stored error rather than raising (for CRUD)."""
    try:
        return await ctx.compiler.compile(text), None
    except Exception as e:  # ValueError from the fallback, or an unexpected LLM error
        return None, str(e)


# ── endpoints ────────────────────────────────────────────────────────────────
@router.get("/rules")
async def list_rules(request: Request) -> list[Rule]:
    return request.app.state.ctx.db.list_rules()


@router.post("/rules/preview")
async def preview_rule(body: PreviewBody, request: Request) -> CompiledRule:
    """Compile-only, no persistence. A true parse failure is a 400."""
    try:
        return await request.app.state.ctx.compiler.compile(body.text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/rules")
async def create_rule(body: CreateBody, request: Request) -> Rule:
    ctx = request.app.state.ctx
    compiled, err = await _compile(ctx, body.text)
    rule = Rule(
        id=uuid4().hex[:8], text=body.text, enabled=body.enabled,
        compiled=compiled, compile_error=err, source="api",
    )
    ctx.db.upsert_rule(rule)          # upsert stamps created_at/updated_at in place
    await ctx.engine.reload_rules()
    return rule


@router.put("/rules/{rid}")
async def update_rule(rid: str, body: UpdateBody, request: Request) -> Rule:
    ctx = request.app.state.ctx
    rule = ctx.db.get_rule(rid)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"no such rule: {rid}")
    if body.text is not None and body.text != rule.text:
        rule.text = body.text
        rule.compiled, rule.compile_error = await _compile(ctx, body.text)
    if body.enabled is not None:
        rule.enabled = body.enabled
    ctx.db.upsert_rule(rule)
    await ctx.engine.reload_rules()
    return rule


@router.post("/rules/{rid}/toggle")
async def toggle_rule(rid: str, request: Request) -> Rule:
    ctx = request.app.state.ctx
    rule = ctx.db.get_rule(rid)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"no such rule: {rid}")
    rule.enabled = not rule.enabled
    ctx.db.upsert_rule(rule)
    await ctx.engine.reload_rules()
    return rule


@router.delete("/rules/{rid}")
async def delete_rule(rid: str, request: Request) -> dict:
    ctx = request.app.state.ctx
    if ctx.db.get_rule(rid) is None:
        raise HTTPException(status_code=404, detail=f"no such rule: {rid}")
    ctx.db.delete_rule(rid)
    await ctx.engine.reload_rules()
    return {"ok": True, "id": rid}


@router.post("/rules/{rid}/run")
async def run_rule(rid: str, request: Request, body: Optional[RunBody] = None) -> dict:
    """Fire a rule by hand. Deterministic rules run their compiled actions; DYNAMIC rules
    are evaluated live by the LLM against the current world state. dry=True never applies."""
    ctx = request.app.state.ctx
    rule = ctx.db.get_rule(rid)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"no such rule: {rid}")
    dry = bool(body.dry) if body else False

    if rule.compiled and rule.compiled.dynamic:
        if not (ctx.evaluator and ctx.evaluator.available()):
            raise HTTPException(status_code=400,
                                detail="dynamic rule needs an LLM backend (set ANTHROPIC_API_KEY or install the claude CLI)")
        if dry:
            snap = await ctx.adguard.snapshot()
            world = await ctx.evaluator.build_world(snap)
            catalog = ctx.evaluator.action_catalog(snap)
            dec = await ctx.evaluator.evaluate_rule(rule, world, catalog)
            return {"ok": True, "dry": True, "dynamic": True,
                    "condition_met": dec.condition_met, "detail": dec.reason,
                    "actions": [a.model_dump(mode="json") for a in dec.actions]}
        res = await ctx.evaluator.evaluate_one(rule, "manual")
        return {"ok": "error" not in res, "dynamic": True,
                "detail": res.get("reason") or res.get("error", ""), **res}

    return await ctx.engine.run_rule(rule, "manual", dry)
