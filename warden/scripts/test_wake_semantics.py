#!/usr/bin/env python3
"""Wake semantics for dynamic rules — who gets evaluated, and when.

No server, no network, no LLM: it builds four rules with different self-scheduled
wake-ups and checks which ones each path would take.

    python scripts/test_wake_semantics.py

The invariant it guards has broken twice. A dynamic rule chooses its own next check
from its own text ("check hourly until 7pm, and stop early once he is on track — don't
check again until the next day"), and that choice has to survive the other things that
can ask for a sweep: the hourly safety-net tick, and a source collection that brought
back something new. The second one was a blanket force, so a rule that had put itself
to sleep until tomorrow was re-evaluated on every poll — disobeying its own instruction
and spending ~22k tokens each time to be told what it had already decided.

The rule now is: a source change may pull a rule FORWARD only if it was about to look
anyway (RuleEvaluator.EARLY_WAKE_WINDOW). A long sleep is an instruction, not a hint.
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from warden.models import utcnow                                    # noqa: E402
from warden.rules.evaluator import RuleEvaluator                    # noqa: E402
from warden.rules.schema import CompiledRule, ManualTrigger, Rule   # noqa: E402


def mk(rid: str, minutes_ahead: int | None) -> Rule:
    when = None if minutes_ahead is None else (
        utcnow() + timedelta(minutes=minutes_ahead)).isoformat()
    return Rule(id=rid, text=f"rule {rid}", enabled=True,
                compiled=CompiledRule(trigger=ManualTrigger(describe="live"), dynamic=True),
                next_check_at=when)


ASLEEP_LONG = mk("asleep-18h", 18 * 60)      # "on track — nothing more until tomorrow 4pm"
DUE_SOON = mk("due-in-30m", 30)              # "not on track — checking hourly"
OVERDUE = mk("overdue", -5)
NEVER_ASKED = mk("no-next-check", None)      # freshly saved, or just recompiled
ALL = [ASLEEP_LONG, DUE_SOON, OVERDUE, NEVER_ASKED]

ev = RuleEvaluator.__new__(RuleEvaluator)     # no ctx wiring needed for selection
ev.ctx = types.SimpleNamespace(db=types.SimpleNamespace(list_rules=lambda: ALL))
ev.available = lambda: False   # stops each sweep right after the filter, so the
                               # selection itself is what we observe — never an LLM call


async def selected(**kw) -> list[str]:
    """The rules a sweep would take: [] means none, a 'skipped' entry means it took one."""
    picked = []
    for rule in ALL:
        ev.ctx.db.list_rules = lambda r=rule: [r]
        if await ev.evaluate_all("wake-test", **kw):
            picked.append(rule.id)
    ev.ctx.db.list_rules = lambda: ALL
    return picked


async def main() -> int:
    print("rule             is_due  wakeable_early")
    for r in ALL:
        print(f"  {r.id:14} {str(RuleEvaluator.is_due(r)):6}  {RuleEvaluator.wakeable_early(r)}")

    tick = await selected()                       # the safety-net tick
    data = await selected(on_data_change=True)    # a source collected something new
    forced = await selected(force=True)           # a person, or the rule's own wake-up

    checks = [
        (RuleEvaluator.is_due(ASLEEP_LONG), False, "a rule asleep for 18h is not due"),
        (RuleEvaluator.wakeable_early(ASLEEP_LONG), False, "…and new data must NOT wake it"),
        (RuleEvaluator.is_due(DUE_SOON), False, "a rule due in 30m is not due yet"),
        (RuleEvaluator.wakeable_early(DUE_SOON), True, "…but new data may pull it forward"),
        (RuleEvaluator.is_due(OVERDUE), True, "an overdue rule is due"),
        (RuleEvaluator.is_due(NEVER_ASKED), True, "a rule with no wake-up set is due"),
        (tick, ["overdue", "no-next-check"], "the safety-net tick takes only what is due"),
        (data, ["due-in-30m", "overdue", "no-next-check"],
         "new data adds the rule that was about to look — and leaves the sleeper alone"),
        (forced, [r.id for r in ALL], "a forced sweep (person / own wake-up) takes everything"),
    ]

    print(f"\nsafety-net tick        : {tick}")
    print(f"data-change sweep      : {data}")
    print(f"forced sweep           : {forced}\n")
    failed = 0
    for got, want, why in checks:
        ok = got == want
        failed += not ok
        print(("  PASS  " if ok else "  FAIL  ") + why
              + ("" if ok else f"\n          got {got!r}, want {want!r}"))
    print("\n" + ("ALL PASS" if not failed else f"{failed} FAILED"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
