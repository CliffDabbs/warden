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
        # The text drives the cadence, so a rewrite invalidates the wake-up the old
        # text asked for: drop it and let the rule be re-judged at once.
        rule.next_check_at = None
        rule.next_check_reason = None
        ctx.engine.disarm_next_check(rid)
    if body.enabled is not None:
        rule.enabled = body.enabled
        if not body.enabled:
            ctx.engine.disarm_next_check(rid)
    ctx.db.upsert_rule(rule)
    await ctx.engine.reload_rules()
    return rule


@router.post("/rules/{rid}/recompile")
async def recompile_rule(rid: str, request: Request) -> Rule:
    """Compile the rule's stored text again, without changing a word of it.

    PUT only recompiles when the text differs, so a rule that compiled badly — the model
    failed and the keyword parser stood in, and the rule now does less than it says — had
    no way back except editing the text into something else and back again. A compile can
    also simply get better: the same sentence that failed last week may compile properly
    after a fix to the compiler.
    """
    ctx = request.app.state.ctx
    rule = ctx.db.get_rule(rid)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"no such rule: {rid}")
    rule.compiled, rule.compile_error = await _compile(ctx, rule.text)
    # The wake-up the old compile asked for describes the old compile; let it be judged
    # again rather than kept alive by a plan the new one never made.
    rule.next_check_at = None
    rule.next_check_reason = None
    ctx.engine.disarm_next_check(rid)
    ctx.db.upsert_rule(rule)
    await ctx.engine.reload_rules()
    warnings = list((rule.compiled.warnings if rule.compiled else []) or [])
    await ctx.audit("user", "recompile_rule", rid,
                    rule.compile_error or (rule.compiled.summary if rule.compiled else ""),
                    ok=not rule.compile_error and not warnings)
    return rule


@router.post("/rules/{rid}/toggle")
async def toggle_rule(rid: str, request: Request) -> Rule:
    ctx = request.app.state.ctx
    rule = ctx.db.get_rule(rid)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"no such rule: {rid}")
    rule.enabled = not rule.enabled
    if not rule.enabled:
        ctx.engine.disarm_next_check(rid)
    ctx.db.upsert_rule(rule)
    await ctx.engine.reload_rules()
    return rule


@router.delete("/rules/{rid}")
async def delete_rule(rid: str, request: Request) -> dict:
    ctx = request.app.state.ctx
    if ctx.db.get_rule(rid) is None:
        raise HTTPException(status_code=404, detail=f"no such rule: {rid}")
    ctx.engine.disarm_next_check(rid)
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
                    "actions": [a.model_dump(mode="json") for a in dec.actions],
                    # the wake-up it WOULD arm — the whole point of a self-scheduling
                    # rule, so a dry run has to show it
                    "next_check_at": (dec.next_check_at.isoformat()
                                      if dec.next_check_at else None),
                    "next_check_reason": dec.next_check_reason}
        try:
            res = await ctx.evaluator.evaluate_one(rule, "manual")
        except Exception as e:
            # An evaluation failure is information, not a server fault — returning a 500
            # gave the UI an unparseable body and told the operator nothing.
            await ctx.audit(f"rule:{rid}", "rule_eval", rule.text[:80],
                            f"evaluation failed: {type(e).__name__}: {e}", ok=False)
            return {"ok": False, "dynamic": True, "applied": [],
                    "detail": f"evaluation failed: {type(e).__name__}: {e}"}
        return {"ok": "error" not in res, "dynamic": True,
                "detail": res.get("reason") or res.get("error", ""), **res}

    return await ctx.engine.run_rule(rule, "manual", dry)
