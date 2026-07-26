"""RuleEngine — schedules cron rules, reacts to signal rules, executes actions.

A stored Rule (text + CompiledRule AST) fires either:
  * on a cron schedule  — ScheduleTrigger, driven by an AsyncIOScheduler(tz)
  * on a signal edge     — SignalTrigger, driven by a SignalBus subscription

Firing a rule = evaluate its conditions, then dispatch each action to the matching
ctx.adguard method. Every real fire is audited, stamps the rule's last_fired, and
pushes a live snapshot to WS clients via ctx.after_change. A dry run computes the
same action list but touches nothing.

Robustness is a hard requirement: a rule that never compiled, names an unknown
group/service, or carries a bad cron must be recorded (audit ok=False) and skipped —
never allowed to kill the scheduler or a bus emit.
"""
from __future__ import annotations

import logging
from datetime import datetime, time
from typing import TYPE_CHECKING, Any, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..models import Signal, utcnow
from .schema import (
    Action, CompiledRule, Condition, Rule,
    ScheduleTrigger, SignalCondition, SignalTrigger, TimeWindowCondition,
)

if TYPE_CHECKING:  # avoid an import cycle; ctx is wired in main.py
    from ..context import AppContext

log = logging.getLogger("warden.engine")

_JOB_PREFIX = "rule:"          # every scheduler job id we own starts with this


# ── comparison + edge helpers ────────────────────────────────────────────────
def _cmp(value: Any, comparator: str, target: Any) -> bool:
    """Apply a Comparator to a (possibly None) signal value. Never raises."""
    try:
        if comparator == "is_true":
            return bool(value)
        if comparator == "is_false":
            return not bool(value)
        if comparator == "==":
            return value == target
        if comparator == "!=":
            return value != target
        a, b = float(value), float(target)          # numeric comparators
        if comparator == ">":
            return a > b
        if comparator == ">=":
            return a >= b
        if comparator == "<":
            return a < b
        if comparator == "<=":
            return a <= b
    except (TypeError, ValueError):
        return False
    return False


def _trigger_matches(trig: SignalTrigger, signal: Signal, changed: bool,
                     prev: Optional[Signal]) -> bool:
    """Does this emitted signal satisfy a SignalTrigger's key + edge + comparator?

    We derive the transition straight from (prev, signal) rather than the bus's edge
    string, so first-ever emits (prev is None) count as a transition — that's what
    makes the "simulate Luke finished Atom" demo fire the YouTube rule.
    """
    if trig.signal != signal.key:
        return False
    if not _cmp(signal.value, trig.comparator, trig.value):
        return False
    edge = trig.edge
    if edge in ("changes", "on_value"):
        return changed
    now_true = bool(signal.value)
    prev_true = bool(prev.value) if prev is not None else None
    if edge == "becomes_true":
        return now_true and (prev is None or not prev_true)
    if edge == "becomes_false":
        return (not now_true) and (prev is None or prev_true)
    return False


def _parse_hhmm(s: str) -> Optional[time]:
    try:
        hh, mm = s.split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


# ── the engine ────────────────────────────────────────────────────────────────
class RuleEngine:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self._tz: str = ctx.settings.tz
        self._zone = ZoneInfo(self._tz)
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._subscribed = False        # subscribe to the bus exactly once
        self._started = False

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Build the scheduler, subscribe to the bus, schedule enabled cron rules."""
        if self._scheduler is None:
            self._scheduler = AsyncIOScheduler(
                timezone=self._tz,
                job_defaults={"misfire_grace_time": 60, "coalesce": True},
            )
        if not self._scheduler.running:
            self._scheduler.start()
        if not self._subscribed:
            # the bus calls this synchronously and awaits the coroutine we return
            self.ctx.bus.subscribe(self._on_signal)
            self._subscribed = True
        # periodic re-evaluation of DYNAMIC (LLM-evaluated) rules; source refreshes
        # trigger it too (registry), this tick is the time-based safety net.
        every = max(1, int(self.ctx.settings.eval_interval_min))
        self._scheduler.add_job(self._eval_tick, IntervalTrigger(minutes=every),
                                id="eval:tick", replace_existing=True)
        self._started = True
        await self.reload_rules()
        log.info("rule engine started (tz=%s, dynamic-eval every %dm)", self._tz, every)

    async def _eval_tick(self) -> None:
        """Interval job: re-evaluate dynamic rules against the current world state."""
        if not self._started or self.ctx.evaluator is None:
            return
        try:
            await self.ctx.evaluator.evaluate_all("tick")
        except Exception:
            log.exception("dynamic-rule evaluation tick failed")

    async def stop(self) -> None:
        self._started = False
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        self._scheduler = None
        # NB: SignalBus has no unsubscribe; _started guards late signal callbacks.

    async def reload_rules(self) -> None:
        """Re-read rules from the DB and rebuild all cron jobs cleanly.

        Signal-triggered rules need no rescheduling — the bus subscriber matches
        against the live DB on every emit — so we only (re)build schedule jobs here.
        """
        if self._scheduler is None:
            return
        # drop the jobs we own, then re-add from the current rule set
        for job in list(self._scheduler.get_jobs()):
            if job.id.startswith(_JOB_PREFIX):
                self._scheduler.remove_job(job.id)

        scheduled = 0
        for rule in self.ctx.db.list_rules():
            if not rule.enabled or rule.compiled is None:
                continue
            if rule.compiled.dynamic:            # dynamic rules run via the evaluator, not cron
                continue
            trig = rule.compiled.trigger
            if not isinstance(trig, ScheduleTrigger):
                continue
            try:
                cron = CronTrigger.from_crontab(trig.cron, timezone=self._tz)
            except Exception as e:  # malformed cron — record + skip, never crash
                await self.ctx.audit(f"rule:{rule.id}", "rule_error", rule.text,
                                     f"bad schedule '{trig.cron}': {e}", ok=False)
                continue
            self._scheduler.add_job(
                self._run_scheduled, cron, args=[rule.id],
                id=f"{_JOB_PREFIX}{rule.id}", replace_existing=True,
            )
            scheduled += 1
        log.info("scheduled %d cron rule(s)", scheduled)

    # ── trigger callbacks ─────────────────────────────────────────────────────
    async def _run_scheduled(self, rule_id: str) -> None:
        """APScheduler job body — fetch the latest rule and fire it."""
        if not self._started:
            return
        rule = self.ctx.db.get_rule(rule_id)
        if rule is None or not rule.enabled:
            return
        try:
            await self.run_rule(rule, reason="schedule")
        except Exception as e:  # last-ditch guard so the scheduler survives
            log.exception("scheduled rule %s crashed", rule_id)
            await self.ctx.audit(f"rule:{rule_id}", "rule_error", rule.text,
                                 f"run failed: {e}", ok=False)

    def _on_signal(self, signal: Signal, changed: bool,
                   prev: Optional[Signal]):
        """SignalBus subscriber. Returns a coroutine the bus awaits."""
        return self._handle_signal(signal, changed, prev)

    async def _handle_signal(self, signal: Signal, changed: bool,
                             prev: Optional[Signal]) -> None:
        """Fire every enabled signal-rule whose trigger matches this emit.

        Robust to a signal with no matching rule (the loop simply finds none) and
        to a single rule blowing up (it's audited, the rest still run).
        """
        if not self._started:
            return
        try:
            rules = self.ctx.db.list_rules()
        except Exception:
            log.exception("could not load rules for signal %s", signal.key)
            return
        for rule in rules:
            if not rule.enabled or rule.compiled is None:
                continue
            if rule.compiled.dynamic:            # dynamic rules run via the evaluator, not signals
                continue
            trig = rule.compiled.trigger
            if not isinstance(trig, SignalTrigger):
                continue
            try:
                if _trigger_matches(trig, signal, changed, prev):
                    await self.run_rule(rule, reason=f"signal:{signal.key}")
            except Exception as e:
                log.exception("signal rule %s crashed", rule.id)
                await self.ctx.audit(f"rule:{rule.id}", "rule_error", rule.text,
                                     f"signal handling failed: {e}", ok=False)

    # ── execution ─────────────────────────────────────────────────────────────
    async def run_rule(self, rule: Rule, reason: str, dry: bool = False) -> dict:
        """Evaluate conditions then run (or, if dry, just describe) the actions.

        Returns {ok, detail, actions:[...]} — actions carry per-action ok/error so
        the UI can show exactly what happened.
        """
        compiled: Optional[CompiledRule] = rule.compiled
        if compiled is None:
            detail = f"rule not compiled: {rule.compile_error or 'no compiled body'}"
            if not dry:
                await self.ctx.audit(f"rule:{rule.id}", "rule_error", rule.text,
                                     detail, ok=False)
            return {"ok": False, "detail": detail, "actions": []}

        # conditions gate real AND dry runs — a rule "wouldn't run" if they fail
        cond_ok, why = self._eval_conditions(compiled.conditions)
        if not cond_ok:
            return {"ok": True, "skipped": True,
                    "detail": f"conditions not met: {why}", "actions": []}

        # dry run: compute the action list without touching AdGuard or the DB
        if dry:
            return {
                "ok": True, "dry": True,
                "detail": f"[{reason}] would run {len(compiled.actions)} action(s)",
                "actions": [a.model_dump(mode="json") for a in compiled.actions],
            }

        # real run: dispatch each action, collecting per-action outcomes
        results: list[dict] = []
        errors: list[str] = []
        for a in compiled.actions:
            d = a.model_dump(mode="json")
            try:
                await self._execute(a)
                d["ok"] = True
            except Exception as e:
                d["ok"] = False
                d["error"] = str(e)
                errors.append(f"{a.kind}: {e}")
            results.append(d)

        ok = not errors
        parts = [self._describe_action(a) for a in compiled.actions] or ["no actions"]
        detail = f"[{reason}] " + "; ".join(parts)
        if errors:
            detail += " | errors: " + "; ".join(errors)

        # audit + stamp + push live update (interface contract)
        await self.ctx.audit(f"rule:{rule.id}", "rule_fired", rule.text, detail, ok=ok)
        self.ctx.db.touch_rule(rule.id, utcnow().isoformat(),
                               "ok" if ok else f"error: {'; '.join(errors)}")
        await self.ctx.after_change("rule")
        return {"ok": ok, "detail": detail, "actions": results}

    async def _execute(self, action: Action) -> None:
        """Dispatch one action to the matching AdGuardService method.

        Group/service/client names are validated against config first, so an unknown
        target raises a clear ValueError (caught by run_rule → audited ok=False)
        rather than a murky failure deep in the AdGuard layer.
        """
        ag = self.ctx.adguard
        if ag is None:
            raise RuntimeError("AdGuard service not available")
        k = action.kind
        if k == "set_service":
            self._require_group(action.group)
            self._require_service(action.service)
            await ag.set_service(action.group, action.service, action.state)
        elif k == "set_group":
            self._require_group(action.group)
            await ag.set_group(action.group, action.state)
        elif k == "set_client":
            self._require_client(action.client)
            await ag.set_client(action.client, action.state)
        elif k == "set_protection":
            await ag.set_protection(action.enabled)
        elif k == "add_rule":
            await ag.add_rule(action.rule)
        elif k == "remove_rule":
            await ag.remove_rule(action.rule)
        else:  # discriminated union makes this unreachable, but stay defensive
            raise ValueError(f"unknown action kind: {k}")

    # ── conditions ────────────────────────────────────────────────────────────
    def _eval_conditions(self, conditions: list[Condition]) -> tuple[bool, str]:
        """All conditions must hold. Returns (ok, reason-if-not)."""
        for c in conditions:
            if isinstance(c, SignalCondition):
                sig = self.ctx.bus.get(c.signal)
                value = sig.value if sig is not None else None
                if not _cmp(value, c.comparator, c.value):
                    return False, f"signal {c.signal} not {c.comparator}"
            elif isinstance(c, TimeWindowCondition):
                if not self._in_time_window(c):
                    return False, "outside time window"
        return True, "ok"

    def _in_time_window(self, c: TimeWindowCondition) -> bool:
        now = datetime.now(self._zone)
        if c.weekdays is not None and now.weekday() not in c.weekdays:
            return False
        t = now.time()
        if c.after:
            after = _parse_hhmm(c.after)
            if after is not None and t < after:
                return False
        if c.before:
            before = _parse_hhmm(c.before)
            if before is not None and t > before:
                return False
        return True

    # ── config validation ─────────────────────────────────────────────────────
    def _require_group(self, name: str) -> None:
        if self.ctx.config.group(name) is None:
            raise ValueError(f"unknown group: {name}")

    def _require_service(self, sid: str) -> None:
        if self.ctx.config.service(sid) is None:
            raise ValueError(f"unknown service: {sid}")

    def _require_client(self, name: str) -> None:
        if not any(c.name == name for c in self.ctx.config.clients):
            raise ValueError(f"unknown client: {name}")

    # ── describe ──────────────────────────────────────────────────────────────
    @staticmethod
    def _describe_action(a: Action) -> str:
        k = a.kind
        if k == "set_service":
            return f"{a.group}/{a.service}={a.state.value}"
        if k == "set_group":
            return f"group {a.group}={a.state.value}"
        if k == "set_client":
            return f"client {a.client}={a.state.value}"
        if k == "set_protection":
            return f"protection={'on' if a.enabled else 'off'}"
        if k == "add_rule":
            return f"add_rule {a.rule}"
        if k == "remove_rule":
            return f"remove_rule {a.rule}"
        return k
