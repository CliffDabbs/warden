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

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ..models import ServiceState, utcnow
from .schema import (
    Action, Rule, SetGroupAction, SetHostServiceAction, SetServiceAction,
)

log = logging.getLogger("warden.evaluator")

_SYSTEM = (
    "You are Warden's live rule evaluator for a home parental-control system. You are "
    "given ONE rule in plain English, the current world state, and a menu of available "
    "actions (switches). Decide what to do RIGHT NOW, and decide when you should next "
    "look at this rule.\n"
    "Principles:\n"
    "- Act ONLY if the rule's condition is clearly satisfied by the data. When unsure, do nothing.\n"
    "- Use ONLY actions from the provided menu; never invent groups/services.\n"
    "- Be idempotent: do NOT return an action whose target is already in the wanted state "
    "(each action lists its 'current' state).\n"
    "- Respect time conditions using the provided 'now' (already in the household's timezone).\n"
    "- If data needed to judge the rule is missing (e.g. a score that isn't available), do "
    "nothing and say so in the reason.\n"
    "- The world may list 'parent_holds': switches a parent set BY HAND. Those are "
    "off-limits — never return an action for a held switch; the hold clears at the "
    "next scheduled reset (e.g. the 8pm switchoff) and the switch returns to your "
    "control. Mention a hold in your reason only when it changes your answer.\n"
    "\nSCHEDULING YOURSELF — this is as important as the decision:\n"
    "The rule's text may describe WHEN it should be checked as well as what to do "
    '(e.g. "no need to check while he is at school", "from 4pm check hourly until 7pm", '
    '"then at 8pm block it again"). Honour that. Return "next_check_local" as the moment you '
    "should next be consulted, and you will be woken exactly then — nothing else will "
    "evaluate this rule before it, so do not rely on some other periodic check existing.\n"
    '- Format: LOCAL wall-clock "YYYY-MM-DD HH:MM", in the same timezone as the "now" you '
    "were given. Do NOT add a UTC offset or convert anything — write the time as a person "
    "in this household would say it. 4pm today is \"2026-07-27 16:00\".\n"
    "- It must be the SOONEST future moment the rule could change its answer. Work forward "
    "from 'now', one step:\n"
    '    * Inside a recurring window ("hourly until 7pm") and it is now 09:25 -> the answer '
    'is TODAY 10:00. It is NOT the end of the window, and NOT after it. Only once "now" is '
    "past the window's end do you move to the next day's start time.\n"
    "    * Before a window starts -> the window's start time.\n"
    "    * A one-off scheduled action (an 8pm re-block) -> that time, if it is sooner.\n"
    "- Whichever of those is EARLIEST wins.\n"
    "- If the rule is satisfied and nothing more can change today, wake at the next "
    "meaningful boundary (e.g. tomorrow morning), not in a few minutes.\n"
    "- Use the school/term information to skip pointless checks, and say so in "
    "next_check_reason.\n"
    "- NEVER infer whether a future date is a school day. Look it up: the weduc source's "
    "state carries `school.upcoming_days` (a per-date list with is_school_day and why) "
    "and `school.next_school_day`. A weekday in term time can still be an INSET day, and "
    "term periods run past the last school day — so the list is the only reliable answer.\n"
    "- Never schedule sooner than 5 minutes away or more than 7 days out.\n"
    '\nReply with ONLY a JSON object: {"condition_met": <bool>, "reason": "<one sentence>", '
    '"actions": [{"kind":"set_service","group":"<g>","service":"<s>","state":"allowed|blocked"} '
    'or {"kind":"set_group","group":"<g>","state":"allowed|blocked"}], '
    '"next_check_local": "<YYYY-MM-DD HH:MM, local wall clock, no offset>", '
    '"next_check_reason": "<short why, naming the current time you worked from>"}. '
    "Empty actions list = no change."
)

# Guard rails on whatever the model returns for next_check_at. A too-soon value would
# spin the evaluator (and the bill); a too-far one would strand the rule.
_MIN_NEXT_CHECK = timedelta(minutes=5)
_MAX_NEXT_CHECK = timedelta(days=7)
# Used when the model gives us nothing usable — long enough not to spin, short enough
# that a rule is never stranded for a whole day.
_FALLBACK_NEXT_CHECK = timedelta(hours=1)


class Decision:
    def __init__(self, condition_met: bool, reason: str, actions: list[Action], raw: dict,
                 next_check_at: Optional[datetime] = None,
                 next_check_reason: str = "") -> None:
        self.condition_met = condition_met
        self.reason = reason
        self.actions = actions
        self.raw = raw
        self.next_check_at = next_check_at
        self.next_check_reason = next_check_reason


class RuleEvaluator:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self._locks: dict[str, asyncio.Lock] = {}

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
        # The adapters' derived facts (e.g. atom.luke.daily_complete) are the source's own
        # verdict on questions the raw state-of-play can't answer — without them the model
        # sees minutes/topics but no completion flag, and correctly refuses to judge
        # "has Luke finished his Atom learning?". Hand them over alongside the raw state.
        signals = [
            {"key": s.key, "value": s.value, "type": s.type.value if hasattr(s.type, "value") else s.type,
             "source": s.source, "subject": s.subject, "at": str(s.at), "meta": s.meta,
             "describe": next((d.describe for d in self.ctx.config.signals if d.key == s.key), "")}
            for s in self.ctx.bus.all()
        ]
        world = {"now": self._now_str(), "adguard_switches": switches,
                 "sources": sources, "subjects": subjects, "signals": signals}
        holds = self.ctx.db.active_pins(utcnow().isoformat())
        if holds:
            world["parent_holds"] = [
                ({"host_service": svc} if g == "_host" else {"group": g, "service": svc})
                | {"state": row["state"], "set_by": row.get("actor", "user"),
                   "since": row.get("created_at", ""),
                   "note": "manually set; off-limits until the next scheduled reset"}
                for (g, svc), row in holds.items()]
        # Belt and braces alongside the hard guard in evaluate_all: if anything ever
        # reaches the model on non-live data, say so plainly in the payload too.
        stale = self.stale_sources()
        if stale:
            world["DATA_WARNING"] = {
                "message": ("Some source data is NOT live. Do not act on it — take no "
                            "actions and say so in the reason."),
                "sources": stale,
            }
        return world

    def action_catalog(self, snapshot) -> list[dict[str, Any]]:
        cat: list[dict[str, Any]] = [
            {"kind": "set_service", "group": g.name, "service": t.id, "current": t.state.value}
            for g in snapshot.groups for t in g.services
        ]
        for g in snapshot.groups:
            cat.append({"kind": "set_group", "group": g.name,
                        "note": "blocked = turn ALL of this group's services off; allowed = all on"})
        for m in self.ctx.config.managed_services:
            cat.append({
                "kind": "set_host_service", "service": m.id, "name": m.name,
                "note": ("GLOBAL — stops/starts the actual service for EVERYONE in the "
                         "house and ends anything mid-stream. Only use it if the rule "
                         "explicitly asks for it."),
            })
        return cat

    # ── evaluation ───────────────────────────────────────────────────────────
    async def evaluate_rule(self, rule: Rule, world: dict, catalog: list[dict]) -> Decision:
        user = (f"RULE:\n{rule.text}\n\nWORLD STATE:\n{json.dumps(world, indent=2, default=str)}"
                f"\n\nAVAILABLE ACTIONS:\n{json.dumps(catalog, indent=2)}")
        data = await self.ctx.compiler.complete_json(
            _SYSTEM, user, model=(self.ctx.settings.eval_model or None),
            purpose=f"rule_eval:{rule.id}")
        actions = self._parse_actions(data.get("actions") or [])
        nxt, nxt_why = self._parse_next_check(data)
        return Decision(bool(data.get("condition_met")), str(data.get("reason", "")).strip(),
                        actions, data, next_check_at=nxt, next_check_reason=nxt_why)

    def _parse_next_check(self, data: dict) -> tuple[datetime, str]:
        """Read the model's self-chosen wake-up time, clamped to sane bounds.

        This value is load-bearing: it is the ONLY thing that will wake a dynamic rule,
        so a missing or nonsense answer must still produce a usable time rather than
        stranding the rule forever.
        """
        now = utcnow()
        why = str(data.get("next_check_reason") or "").strip()
        try:
            local_tz = ZoneInfo(self.ctx.settings.tz)
        except Exception:
            local_tz = timezone.utc

        # Prefer the offset-free local field. Asking for an offset was a real source of
        # error: the model wrote the intended wall-clock hour but stamped it "+00:00",
        # so during BST every wake-up landed an hour late (an 8am check fired at 9am) —
        # and it was inconsistent about it, converting correctly some runs. Taking a bare
        # local time and attaching the household zone here makes it deterministic.
        raw = data.get("next_check_local") or data.get("next_check_at")
        parsed: Optional[datetime] = None
        if isinstance(raw, str) and raw.strip():
            txt = raw.strip().replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(txt)
            except ValueError:
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
                    try:
                        parsed = datetime.strptime(txt, fmt)
                        break
                    except ValueError:
                        continue
        if parsed is not None and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=local_tz)

        if parsed is None:
            return now + _FALLBACK_NEXT_CHECK, (
                (why + " ") if why else "") + "(no usable next_check_at; defaulted to 1h)"

        parsed = parsed.astimezone(timezone.utc)
        if parsed < now + _MIN_NEXT_CHECK:
            return now + _MIN_NEXT_CHECK, (
                (why + " ") if why else "") + "(clamped: asked to wake too soon)"
        if parsed > now + _MAX_NEXT_CHECK:
            return now + _MAX_NEXT_CHECK, (
                (why + " ") if why else "") + "(clamped: asked to wake >7d out)"
        return parsed, why

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
            elif kind == "set_host_service":
                sid = a.get("service")
                if self.ctx.config.managed_service(sid) is not None:
                    out.append(SetHostServiceAction(
                        service=sid, running=bool(a.get("running", False))))
        return out

    async def apply(self, rule: Rule, decision: Decision) -> list[str]:
        """Execute the decided actions and audit each with the rule's reasoning.

        Manual holds are enforced HERE, not just requested in the prompt: a held
        switch is skipped (set_service) or spared (set_group expands to the group's
        unheld switches), and what was withheld is audited so "why didn't the rule
        close YouTube?" has a visible answer. The parent's hand outranks the model
        until a scheduled rule resets the switch.
        """
        holds = self.ctx.db.active_pins(utcnow().isoformat())
        withheld: list[str] = []
        applied: list[str] = []
        for act in decision.actions:
            try:
                if isinstance(act, SetServiceAction):
                    if (act.group, act.service) in holds:
                        withheld.append(f"{act.group}.{act.service}")
                        continue
                    await self.ctx.adguard.set_service(act.group, act.service, act.state)
                    target = f"{act.group}.{act.service}={act.state.value}"
                elif isinstance(act, SetGroupAction):
                    group_svcs = [t.id for t in self.ctx.config.services_for_group(act.group)]
                    held = [svc for svc in group_svcs if (act.group, svc) in holds]
                    if held:
                        withheld.extend(f"{act.group}.{svc}" for svc in held)
                        # per-service writes are not atomic — keep going on failure so
                        # one bad client write can't half-abandon the rest, and report
                        # what actually happened rather than what was intended
                        wrote, failed = 0, []
                        for svc in group_svcs:
                            if svc in held:
                                continue
                            try:
                                await self.ctx.adguard.set_service(act.group, svc, act.state)
                                wrote += 1
                            except Exception as e:
                                failed.append(f"{svc}: {e}")
                        target = (f"{act.group}({wrote}/{len(group_svcs) - len(held)} unheld, "
                                  f"{len(held)} spared)={act.state.value}")
                        if failed:
                            await self.ctx.audit(f"rule:{rule.id}", "rule_eval", target,
                                                 "partial: " + "; ".join(failed), ok=False)
                            if not wrote:
                                raise RuntimeError("; ".join(failed))
                    else:
                        await self.ctx.adguard.set_group(act.group, act.state)
                        target = f"{act.group}(all)={act.state.value}"
                elif isinstance(act, SetHostServiceAction):
                    if ("_host", act.service) in holds:
                        withheld.append(f"host:{act.service}")
                        continue
                    svc = self.ctx.config.managed_service(act.service)
                    if svc is None or self.ctx.hosts is None:
                        continue
                    await self.ctx.hosts.set_running(svc, act.running)
                    target = f"{act.service}={'running' if act.running else 'stopped'}"
                else:
                    continue
                applied.append(target)
                await self.ctx.audit(f"rule:{rule.id}", "rule_eval", target, decision.reason, ok=True)
            except Exception as e:
                await self.ctx.audit(f"rule:{rule.id}", "rule_eval", str(act), f"{e}", ok=False)
        if withheld:
            await self.ctx.audit(
                f"rule:{rule.id}", "rule_eval", "manual hold",
                f"left alone (set by hand, resets on the next scheduled rule): "
                f"{', '.join(sorted(set(withheld)))}")
        if applied:
            await self.ctx.after_change(f"rule:{rule.id}")
        return applied

    async def evaluate_and_apply(self, rule: Rule, world: dict, catalog: list[dict],
                                 reason_tag: str) -> dict:
        decision = await self.evaluate_rule(rule, world, catalog)
        applied = await self.apply(rule, decision)
        verb = "acted" if applied else "no-op"
        last = f"{verb} ({reason_tag}): {decision.reason}" + (f" -> {', '.join(applied)}" if applied else "")

        nxt_iso = decision.next_check_at.isoformat() if decision.next_check_at else None
        self.ctx.db.touch_rule(rule.id, utcnow().isoformat(), last[:400],
                               next_check_at=nxt_iso,
                               next_check_reason=decision.next_check_reason[:300])
        # arm the one-shot wake-up the model just asked for
        if nxt_iso and self.ctx.engine is not None:
            try:
                self.ctx.engine.arm_next_check(rule.id, decision.next_check_at)
            except Exception:
                pass
        return {"rule_id": rule.id, "condition_met": decision.condition_met,
                "reason": decision.reason, "applied": applied,
                "next_check_at": nxt_iso,
                "next_check_reason": decision.next_check_reason}

    # How close a rule's own next check has to be before newly-collected data may pull
    # it forward. A rule checking hourly is watching for exactly that data and should
    # see it at once; a rule that has said "not until tomorrow" has answered its
    # question for the day and must be left alone.
    EARLY_WAKE_WINDOW = timedelta(hours=2)

    @staticmethod
    def is_due(rule: Rule, now: Optional[datetime] = None) -> bool:
        """Is this rule due for evaluation?

        A dynamic rule's own `next_check_at` is authoritative: while it sits in the
        future the rule is deliberately asleep, and nothing — not the safety-net tick,
        not a source refresh — should wake it. That is what makes instructions like
        "no need to check while he's at school" actually hold, instead of being
        overridden by the hourly Atom poll. (New data can pull a rule forward, but only
        inside EARLY_WAKE_WINDOW — see `wakeable_early`.)
        """
        if not rule.next_check_at:
            return True
        try:
            due = datetime.fromisoformat(rule.next_check_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        return (now or utcnow()) >= due

    @classmethod
    def wakeable_early(cls, rule: Rule, now: Optional[datetime] = None) -> bool:
        """May a source collection that changed something pull this rule forward?

        Only if it was about to look anyway. The early wake exists for the rule that is
        WAITING on the data — Luke finishing at 16:52, collected at 17:00, acted on at
        18:00 was the bug it fixed — and such a rule is checking every hour or so, so
        its next check is minutes away.

        A rule that has just told us "he's on track, stop early, don't check again until
        tomorrow at 4pm" is in the opposite position: it has answered its question for
        the day. Waking it because an unrelated club booking or meal choice moved
        contradicts the instruction in its own text and pays 20k tokens to be told the
        same thing again. So a long sleep is honoured; a short one is not.
        """
        if not rule.next_check_at:
            return True
        try:
            due = datetime.fromisoformat(rule.next_check_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        return (due - (now or utcnow())) <= cls.EARLY_WAKE_WINDOW

    async def _await_collection(self, reason_tag: str) -> None:
        """Let the sources finish gathering before we judge anything on their data.

        A rule's self-scheduled wake-up and the source poll it depends on are both
        anchored to the hour, so they fire together — and this evaluator reads the
        STORED state, which at that moment is still the previous cycle's. The result is
        a rule that acts a full cadence late: Luke's islands landed at 16:52, were
        collected at 17:00, and the rule that watches them acted at 18:00.

        Waiting here (rather than reordering the schedules) also covers the cases a
        cadence tweak wouldn't: a slow scrape, a restart mid-cycle, and a poll the
        schedule says is overdue.
        """
        registry = getattr(self.ctx, "registry", None)
        if registry is None:
            return
        try:
            waited = await registry.ensure_fresh()
        except Exception:
            log.exception("waiting on source data before evaluation failed")
            return
        if waited:
            log.info("evaluation (%s) waited for fresh data from: %s",
                     reason_tag, ", ".join(waited))

    def stale_sources(self) -> list[dict[str, Any]]:
        """Enabled sources whose stored state is not trustworthy live data.

        Dynamic rules flip real network switches, so they must never run on the offline
        sample. This is not hypothetical: an expired Atom session made the adapter serve
        fixtures ("5 islands all-time" against a real 403), the run was logged as a
        success, and the rule kept the kids blocked after Luke had finished his work.
        """
        bad: list[dict[str, Any]] = []
        for s in self.ctx.config.sources:
            if not s.enabled:
                continue
            raw = self.ctx.db.get_kv(f"state:{s.key}")
            if not raw:
                bad.append({"source": s.key, "why": "no data collected yet"})
                continue
            try:
                dq = (json.loads(raw) or {}).get("data_quality") or {}
            except Exception:
                bad.append({"source": s.key, "why": "unreadable stored state"})
                continue
            if dq.get("live") is not True:
                bad.append({"source": s.key,
                            "why": dq.get("note") or "not live data",
                            "collected_at": dq.get("collected_at", "")})
        return bad

    async def evaluate_all(self, reason_tag: str = "tick",
                           only: Optional[str] = None, force: bool = False,
                           on_data_change: bool = False) -> list[dict]:
        """Evaluate dynamic rules that are DUE.

        `only` restricts to a single rule id — used by the per-rule wake-up job.
        `force` ignores the schedule entirely (a person asked, or the rule's own wake-up
        fired). `on_data_change` is the softer kind of urgency: a source collected
        something new, so rules that were about to look get to look now — and rules that
        have deliberately gone to sleep for the rest of the day stay asleep. It used to
        be a blanket force, which meant a rule whose own text says "stop early once he
        is on track" was re-evaluated on every hourly poll regardless, at 22k tokens a
        time, to be told again what it had already decided.
        """
        rules = [r for r in self.ctx.db.list_rules()
                 if r.enabled and r.compiled and r.compiled.dynamic
                 and (only is None or r.id == only)]
        if not force:
            rules = [r for r in rules
                     if self.is_due(r) or (on_data_change and self.wakeable_early(r))]
        if not rules:
            return []
        if not self.available():
            return [{"skipped": "no LLM backend for dynamic rules"}]

        # Data first, decisions second.
        await self._await_collection(reason_tag)

        # Refuse to decide on data we don't trust. Leaving switches where they are is the
        # lesser harm: acting on the sample once left the kids blocked all day.
        stale = self.stale_sources()
        if stale:
            note = "; ".join(f"{s['source']}: {s['why']}" for s in stale)
            for r in rules:
                self.ctx.db.touch_rule(
                    r.id, utcnow().isoformat(),
                    f"skipped ({reason_tag}): source data is not live — {note}"[:400],
                    next_check_at=r.next_check_at, next_check_reason=r.next_check_reason)
            await self.ctx.audit(
                "evaluator", "rule_eval", "all-dynamic",
                f"SKIPPED — refusing to act on non-live data: {note}", ok=False)
            log.warning("dynamic rules skipped; sources not live: %s", note)
            return [{"skipped": "source data is not live", "sources": stale}]
        snapshot = await self.ctx.adguard.snapshot()
        world = await self.build_world(snapshot)
        catalog = self.action_catalog(snapshot)
        results = []
        for r in rules:
            try:
                res = await self._evaluate_guarded(r, world, catalog, reason_tag, force,
                                                   on_data_change)
                if res is not None:
                    results.append(res)
            except Exception as e:
                results.append({"rule_id": r.id, "error": str(e)})
                await self.ctx.audit(f"rule:{r.id}", "rule_eval", r.text, f"evaluation error: {e}", ok=False)
        return results

    # one in-flight evaluation per rule
    _MIN_GAP_SECONDS = 60

    def _lock_for(self, rule_id: str) -> "asyncio.Lock":
        return self._locks.setdefault(rule_id, asyncio.Lock())

    async def _evaluate_guarded(self, rule: Rule, world: dict, catalog: list[dict],
                                reason_tag: str, force: bool,
                                on_data_change: bool = False) -> Optional[dict]:
        """Evaluate a rule at most once per moment, however many paths ask.

        Three schedules legitimately land on the same minute: the rule's own
        self-scheduled wake-up, the safety-net tick, and a source refresh. Each read
        `is_due` before any of them had written an answer, so one 08:00 produced three
        concurrent LLM calls that disagreed with each other in the log. The lock
        serialises them and the re-read makes the losers stand down.
        """
        async with self._lock_for(rule.id):
            fresh = self.ctx.db.get_rule(rule.id) or rule
            # did another path just do this one while we waited for the lock?
            if fresh.last_fired_at:
                try:
                    last = datetime.fromisoformat(fresh.last_fired_at.replace("Z", "+00:00"))
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    gap = (utcnow() - last).total_seconds()
                    if gap < self._MIN_GAP_SECONDS:
                        log.info("rule %s already evaluated %.0fs ago (%s) — skipping "
                                 "duplicate from %s", rule.id, gap,
                                 (fresh.last_result or "")[:60], reason_tag)
                        return None
                except ValueError:
                    pass
            # and re-check dueness against the freshly-read row, by the same rule the
            # caller was admitted under
            if not force and not (self.is_due(fresh)
                                  or (on_data_change and self.wakeable_early(fresh))):
                return None
            return await self.evaluate_and_apply(fresh, world, catalog, reason_tag)

    async def evaluate_one(self, rule: Rule, reason_tag: str = "manual") -> dict:
        """Evaluate + apply a single rule now (used by the 'Evaluate now' button)."""
        if not self.available():
            return {"rule_id": rule.id, "error": "no LLM backend (set ANTHROPIC_API_KEY or install the claude CLI)"}
        # "Evaluate now" means now — including any collection in flight or overdue.
        await self._await_collection(reason_tag)
        # The manual button gets the same hard guard as the scheduler. Relying on the
        # model to notice DATA_WARNING is not a safety mechanism.
        stale = self.stale_sources()
        if stale:
            note = "; ".join(f"{s['source']}: {s['why']}" for s in stale)
            return {"rule_id": rule.id, "skipped": "source data is not live",
                    "sources": stale, "applied": [],
                    "reason": f"refusing to act on non-live data — {note}"}
        snapshot = await self.ctx.adguard.snapshot()
        world = await self.build_world(snapshot)
        catalog = self.action_catalog(snapshot)
        return await self.evaluate_and_apply(rule, world, catalog, reason_tag)
