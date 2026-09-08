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
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
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
_EVAL_PREFIX = "eval:rule:"    # one-shot self-scheduled dynamic-rule checks


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
        trigger, describe = self._eval_trigger(every)
        self._scheduler.add_job(self._eval_tick, trigger,
                                id="eval:tick", replace_existing=True)
        self._started = True
        await self.reload_rules()
        self.restore_next_checks()
        log.info("rule engine started (tz=%s, dynamic-eval %s + self-scheduled)",
                 self._tz, describe)

    def _eval_trigger(self, every: int) -> tuple[Any, str]:
        """Anchor the eval tick to the WALL CLOCK, not to process start.

        An IntervalTrigger counts from whenever the server booted, so a 60-minute
        interval started at 21:47 fires at 22:47, 23:47 … — which makes "when did it
        last check Luke's progress?" unanswerable. Cron-anchoring instead puts the
        tick at predictable times (:00 hourly, :00/:30 half-hourly) that survive a
        restart. Falls back to an interval only for periods that don't divide evenly
        into the clock.
        """
        # Deliberately offset from :00. Rules self-schedule on the hour and sources poll
        # on the hour, so putting this backstop there too meant three paths hitting the
        # same rule in the same minute. It is only a safety net for rules with no wake-up
        # armed, so it loses nothing by running mid-hour.
        if every % 60 == 0:
            hours = every // 60
            spec = "*" if hours == 1 else f"*/{hours}"
            return (CronTrigger(hour=spec, minute=30, timezone=self._tz),
                    "hourly at :30" if hours == 1 else f"every {hours}h at :30")
        if 60 % every == 0:
            return (CronTrigger(minute=f"*/{every}", timezone=self._tz),
                    f"every {every}m on the clock")
        return IntervalTrigger(minutes=every), f"every {every}m from start"

    async def _eval_tick(self) -> None:
        """Safety-net job: evaluate any dynamic rule that is DUE.

        Rules normally wake themselves via the one-shot jobs armed by
        `arm_next_check`. This tick only catches rules with no wake-up armed (freshly
        created, or a job lost to a crash between arming and firing).
        """
        if not self._started or self.ctx.evaluator is None:
            return
        try:
            await self.ctx.evaluator.evaluate_all("tick")
        except Exception:
            log.exception("dynamic-rule evaluation tick failed")

    # ── self-scheduling dynamic rules ────────────────────────────────────────
    def arm_next_check(self, rule_id: str, when: datetime) -> None:
        """Schedule the one-shot wake-up a dynamic rule asked for.

        Replaces any wake-up already armed for the rule, so the most recent
        evaluation always wins.
        """
        if self._scheduler is None:
            return
        self._scheduler.add_job(
            self._eval_one_job, DateTrigger(run_date=when), args=[rule_id],
            id=f"{_EVAL_PREFIX}{rule_id}", replace_existing=True,
            misfire_grace_time=3600, coalesce=True,
        )
        log.info("rule %s next check armed for %s", rule_id, when.isoformat())

    def disarm_next_check(self, rule_id: str) -> None:
        if self._scheduler is None:
            return
        try:
            self._scheduler.remove_job(f"{_EVAL_PREFIX}{rule_id}")
        except Exception:
            pass

    async def _eval_one_job(self, rule_id: str) -> None:
        """A rule's own wake-up fired: evaluate just that rule, and let it re-arm."""
        if not self._started or self.ctx.evaluator is None:
            return
        try:
            await self.ctx.evaluator.evaluate_all("self-scheduled", only=rule_id, force=True)
        except Exception:
            log.exception("self-scheduled evaluation of rule %s failed", rule_id)

    # ── timed revert for quick actions ───────────────────────────────────────
    def arm_quick_revert(self, aid: str, label: str,
                         prior: list[tuple[str, str, Any]], minutes: int) -> None:
        """Put the switches a quick action changed back where they were, later.

        In-memory only and deliberately so: if Warden is down at the revert time the
        safest outcome is that the operator notices, not that a stale revert fires at
        an arbitrary moment after a restart.
        """
        if self._scheduler is None:
            return
        when = utcnow() + timedelta(minutes=max(1, int(minutes)))
        self._scheduler.add_job(
            self._quick_revert_job, DateTrigger(run_date=when),
            args=[aid, label, prior, utcnow().isoformat()],
            id=f"quick:revert:{aid}", replace_existing=True,
            misfire_grace_time=600, coalesce=True,
        )
        log.info("quick action %s will revert at %s", aid, when.isoformat())

    async def _quick_revert_job(self, aid: str, label: str,
                                prior: list[tuple[str, str, Any]],
                                armed_at: str = "") -> None:
        # a pin created AFTER the quick action means someone changed their mind by
        # hand in the meantime — that newer choice outranks this revert
        pins = self.ctx.db.active_pins(utcnow().isoformat())
        restored: list[str] = []
        skipped: list[str] = []
        for group, service, state in prior:
            pin = pins.get((group, service))
            if pin and armed_at and (pin.get("created_at") or "") > armed_at:
                skipped.append(f"{group}/{service}")
                continue
            try:
                await self.ctx.adguard.set_service(group, service, state)
                # the manual window is over: putting the switch back releases its hold
                self.ctx.db.clear_pins([(group, service)])
                restored.append(f"{group}/{service}={getattr(state, 'value', state)}")
            except Exception as e:
                await self.ctx.audit("scheduler", "quick_revert", f"{aid}:{group}/{service}",
                                     f"failed: {e}", ok=False)
        note = f"reverted {label}: {', '.join(restored) or 'nothing'}"
        if skipped:
            note += f" | left alone (newer manual hold): {', '.join(skipped)}"
        await self.ctx.audit("scheduler", "quick_revert", aid, note)
        await self.ctx.after_change("quick_revert")

    def restore_next_checks(self) -> None:
        """Re-arm wake-ups from the db after a restart.

        Anything already overdue is nudged a few seconds out rather than dropped, so a
        restart can't silently lose a rule's pending check.
        """
        if self._scheduler is None or self.ctx.evaluator is None:
            return
        now = utcnow()
        armed = 0
        for r in self.ctx.db.list_rules():
            if not (r.enabled and r.compiled and r.compiled.dynamic and r.next_check_at):
                continue
            try:
                when = datetime.fromisoformat(r.next_check_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=self._zone)
            self.arm_next_check(r.id, max(when, now + timedelta(seconds=10)))
            armed += 1
        if armed:
            log.info("restored %d self-scheduled rule check(s)", armed)

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
        # Signal-fired rules are automation reacting to events, so a parent's manual
        # hold outranks them (they SPARE held switches). Schedule- and manually-run
        # rules are the parent's own arrangements — they apply and RESET the holds.
        fired_by = "signal" if reason.startswith("signal:") else "reset"
        parts: list[str] = []
        for a in compiled.actions:
            d = a.model_dump(mode="json")
            try:
                note = await self._execute(a, fired_by)
                d["ok"] = True
                if note:
                    d["note"] = note
                parts.append(self._describe_action(a) + (f" [{note}]" if note else ""))
            except Exception as e:
                d["ok"] = False
                d["error"] = str(e)
                errors.append(f"{a.kind}: {e}")
                parts.append(self._describe_action(a))
            results.append(d)

        ok = not errors
        parts = parts or ["no actions"]
        detail = f"[{reason}] " + "; ".join(parts)  # noqa: E501 — parts built above
        if errors:
            detail += " | errors: " + "; ".join(errors)

        # audit + stamp + push live update (interface contract)
        await self.ctx.audit(f"rule:{rule.id}", "rule_fired", rule.text, detail, ok=ok)
        self.ctx.db.touch_rule(rule.id, utcnow().isoformat(),
                               "ok" if ok else f"error: {'; '.join(errors)}")
        await self.ctx.after_change("rule")
        return {"ok": ok, "detail": detail, "actions": results}

    async def _execute(self, action: Action, fired_by: str = "reset") -> Optional[str]:
        """Dispatch one action to the matching AdGuardService method.

        Group/service/client names are validated against config first, so an unknown
        target raises a clear ValueError (caught by run_rule → audited ok=False)
        rather than a murky failure deep in the AdGuard layer.

        `fired_by` decides how manual holds are treated:
          * "reset"  (cron schedule, or a human pressing Run now): the write is the
            reset a hold waits for. Pins are cleared BEFORE the write — the reset
            moment has passed either way, and clearing after meant a transient
            AdGuard error stranded the pin, locking the evaluator out of the very
            switch it could have repaired.
          * "signal" (an automated event): automation must not undo a parent's hand —
            held switches are spared, and the spared set is reported in the note.
        Returns a short note for the audit line, or None.
        """
        ag = self.ctx.adguard
        if ag is None:
            raise RuntimeError("AdGuard service not available")
        holds = (self.ctx.db.active_pins(utcnow().isoformat())
                 if fired_by == "signal" else {})
        k = action.kind
        if k == "set_service":
            self._require_group(action.group)
            self._require_service(action.service)
            if (action.group, action.service) in holds:
                return "spared: manual hold"
            if fired_by == "reset":
                self.ctx.db.clear_pins([(action.group, action.service)])
            await ag.set_service(action.group, action.service, action.state)
        elif k == "set_group":
            self._require_group(action.group)
            group_svcs = [svc.id for svc in self.ctx.config.services_for_group(action.group)]
            held = [svc for svc in group_svcs if (action.group, svc) in holds]
            if held:
                for svc in group_svcs:
                    if svc not in held:
                        await ag.set_service(action.group, svc, action.state)
                return f"spared {len(held)} manually-held switch(es)"
            if fired_by == "reset":
                self.ctx.db.clear_pins([(action.group, svc) for svc in group_svcs])
            await ag.set_group(action.group, action.state)
        elif k == "set_client":
            await self._require_client(ag, action.client)
            await ag.set_client(action.client, action.state)
        elif k == "set_protection":
            await ag.set_protection(action.enabled)
        elif k == "add_rule":
            await ag.add_rule(action.rule)
        elif k == "remove_rule":
            await ag.remove_rule(action.rule)
        elif k == "set_host_service":
            svc = self.ctx.config.managed_service(action.service)
            if svc is None:
                raise ValueError(f"unknown managed service: {action.service}")
            if self.ctx.hosts is None:
                raise RuntimeError("host control not available")
            if ("_host", action.service) in holds:
                return "spared: manual hold"
            if fired_by == "reset":
                self.ctx.db.clear_pins([("_host", action.service)])
            await self.ctx.hosts.set_running(svc, action.running)
        else:  # discriminated union makes this unreachable, but stay defensive
            raise ValueError(f"unknown action kind: {k}")
        return None

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

    async def _require_client(self, ag, name: str) -> None:
        # Resolved, not config-only: a device can join a group by AdGuard tag.
        if not any(c.name == name for c in await ag.resolve_clients()):
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
        if k == "set_host_service":
            return f"{a.service}={'running' if a.running else 'stopped'} (whole house)"
        return k
