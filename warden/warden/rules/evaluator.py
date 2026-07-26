"""Dynamic rule evaluator — the LLM decides what to do, live, from the world state.

For rules that depend on source data ("when Luke has done 30 mins and scored 80%"),
we don't compile to a fixed signal. Instead, on a cadence (each source refresh + a
periodic tick), we hand the LLM:
  * the rule in plain English,
  * the current world state (now, AdGuard switches, each source's "state of play"),
  * the menu of available actions (the switches + their current state),
and it returns which actions to take (from that menu only) + a one-line reason. We
validate the actions against config, apply them idempotently, and audit the decision
*with its reasoning* so the Activity feed shows why a switch flipped.

Deterministic time/action rules ("at 8pm disable kids") are NOT handled here — the
engine runs those on cron. This evaluator only touches rules whose compiled form has
`dynamic=True`. It needs an LLM backend (API key or the claude CLI); with neither, it
no-ops with a clear note.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ..models import ServiceState, utcnow
from .schema import Action, Rule, SetGroupAction, SetServiceAction

_SYSTEM = (
    "You are Warden's live rule evaluator for a home parental-control system. You are "
    "given ONE rule in plain English, the current world state, and a menu of available "
    "actions (switches). Decide what to do RIGHT NOW.\n"
    "Principles:\n"
    "- Act ONLY if the rule's condition is clearly satisfied by the data. When unsure, do nothing.\n"
    "- Use ONLY actions from the provided menu; never invent groups/services.\n"
    "- Be idempotent: do NOT return an action whose target is already in the wanted state "
    "(each action lists its 'current' state).\n"
    "- Respect time conditions using the provided 'now'.\n"
    "- If data needed to judge the rule is missing (e.g. a score that isn't available), do "
    "nothing and say so in the reason.\n"
    'Reply with ONLY a JSON object: {"condition_met": <bool>, "reason": "<one sentence>", '
    '"actions": [{"kind":"set_service","group":"<g>","service":"<s>","state":"allowed|blocked"} '
    'or {"kind":"set_group","group":"<g>","state":"allowed|blocked"}]}. Empty actions list = no change.'
)


class Decision:
    def __init__(self, condition_met: bool, reason: str, actions: list[Action], raw: dict) -> None:
        self.condition_met = condition_met
        self.reason = reason
        self.actions = actions
        self.raw = raw


class RuleEvaluator:
    def __init__(self, ctx) -> None:
        self.ctx = ctx

    def available(self) -> bool:
        return bool(self.ctx.compiler and self.ctx.compiler.has_llm())

    # ── context assembly ─────────────────────────────────────────────────────
    def _now_str(self) -> str:
        try:
            tz = ZoneInfo(self.ctx.settings.tz)
        except Exception:
            tz = ZoneInfo("UTC")
        return datetime.now(tz).strftime("%Y-%m-%d %H:%M (%A)")

    async def build_world(self, snapshot) -> dict[str, Any]:
        switches = [
            {"group": g.name, "service": t.id, "state": t.state.value}
            for g in snapshot.groups for t in g.services
        ]
        sources: dict[str, Any] = {}
        for s in self.ctx.config.sources:
            raw = self.ctx.db.get_kv(f"state:{s.key}")
            if raw:
                try:
                    sources[s.key] = json.loads(raw)
                except Exception:
                    pass
        subjects = [
            {"id": s.id, "name": s.name,
             "contexts": [c.model_dump(exclude_none=True) for c in s.contexts]}
            for s in self.ctx.config.subjects
        ]
        return {"now": self._now_str(), "adguard_switches": switches,
                "sources": sources, "subjects": subjects}

    def action_catalog(self, snapshot) -> list[dict[str, Any]]:
        cat: list[dict[str, Any]] = [
            {"kind": "set_service", "group": g.name, "service": t.id, "current": t.state.value}
            for g in snapshot.groups for t in g.services
        ]
        for g in snapshot.groups:
            cat.append({"kind": "set_group", "group": g.name,
                        "note": "blocked = turn ALL of this group's services off; allowed = all on"})
        return cat

    # ── evaluation ───────────────────────────────────────────────────────────
    async def evaluate_rule(self, rule: Rule, world: dict, catalog: list[dict]) -> Decision:
        user = (f"RULE:\n{rule.text}\n\nWORLD STATE:\n{json.dumps(world, indent=2, default=str)}"
                f"\n\nAVAILABLE ACTIONS:\n{json.dumps(catalog, indent=2)}")
        data = await self.ctx.compiler.complete_json(_SYSTEM, user)
        actions = self._parse_actions(data.get("actions") or [])
        return Decision(bool(data.get("condition_met")), str(data.get("reason", "")).strip(),
                        actions, data)

    def _parse_actions(self, raw_actions: list) -> list[Action]:
        """Validate LLM-returned actions against the real config vocabulary."""
        groups = {g.name for g in self.ctx.config.groups}
        out: list[Action] = []
        for a in raw_actions:
            if not isinstance(a, dict):
                continue
            kind = a.get("kind")
            state = ServiceState.blocked if a.get("state") == "blocked" else ServiceState.allowed
            if kind == "set_service":
                g, svc = a.get("group"), a.get("service")
                if g in groups and self.ctx.config.service(svc) and svc in \
                        [s.id for s in self.ctx.config.services_for_group(g)]:
                    out.append(SetServiceAction(group=g, service=svc, state=state))
            elif kind == "set_group":
                if a.get("group") in groups:
                    out.append(SetGroupAction(group=a["group"], state=state))
        return out

    async def apply(self, rule: Rule, decision: Decision) -> list[str]:
        """Execute the decided actions and audit each with the rule's reasoning."""
        applied: list[str] = []
        for act in decision.actions:
            try:
                if isinstance(act, SetServiceAction):
                    await self.ctx.adguard.set_service(act.group, act.service, act.state)
                    target = f"{act.group}.{act.service}={act.state.value}"
                elif isinstance(act, SetGroupAction):
                    await self.ctx.adguard.set_group(act.group, act.state)
                    target = f"{act.group}(all)={act.state.value}"
                else:
                    continue
                applied.append(target)
                await self.ctx.audit(f"rule:{rule.id}", "rule_eval", target, decision.reason, ok=True)
            except Exception as e:
                await self.ctx.audit(f"rule:{rule.id}", "rule_eval", str(act), f"{e}", ok=False)
        if applied:
            await self.ctx.after_change(f"rule:{rule.id}")
        return applied

    async def evaluate_and_apply(self, rule: Rule, world: dict, catalog: list[dict],
                                 reason_tag: str) -> dict:
        decision = await self.evaluate_rule(rule, world, catalog)
        applied = await self.apply(rule, decision)
        verb = "acted" if applied else "no-op"
        last = f"{verb} ({reason_tag}): {decision.reason}" + (f" -> {', '.join(applied)}" if applied else "")
        self.ctx.db.touch_rule(rule.id, utcnow().isoformat(), last[:400])
        return {"rule_id": rule.id, "condition_met": decision.condition_met,
                "reason": decision.reason, "applied": applied}

    async def evaluate_all(self, reason_tag: str = "tick") -> list[dict]:
        rules = [r for r in self.ctx.db.list_rules()
                 if r.enabled and r.compiled and r.compiled.dynamic]
        if not rules:
            return []
        if not self.available():
            return [{"skipped": "no LLM backend for dynamic rules"}]
        snapshot = await self.ctx.adguard.snapshot()
        world = await self.build_world(snapshot)
        catalog = self.action_catalog(snapshot)
        results = []
        for r in rules:
            try:
                results.append(await self.evaluate_and_apply(r, world, catalog, reason_tag))
            except Exception as e:
                results.append({"rule_id": r.id, "error": str(e)})
                await self.ctx.audit(f"rule:{r.id}", "rule_eval", r.text, f"evaluation error: {e}", ok=False)
        return results

    async def evaluate_one(self, rule: Rule, reason_tag: str = "manual") -> dict:
        """Evaluate + apply a single rule now (used by the 'Evaluate now' button)."""
        if not self.available():
            return {"rule_id": rule.id, "error": "no LLM backend (set ANTHROPIC_API_KEY or install the claude CLI)"}
        snapshot = await self.ctx.adguard.snapshot()
        world = await self.build_world(snapshot)
        catalog = self.action_catalog(snapshot)
        return await self.evaluate_and_apply(rule, world, catalog, reason_tag)
