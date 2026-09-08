"""Compiled rule AST — what the plain-English compiler produces and the engine runs.

A rule is: WHEN <trigger> [IF <conditions>] THEN <actions>.

Triggers
  schedule : a cron expression      -> "at 8pm every day"
  signal   : a signal crossing/edge  -> "when luke has completed his atom learning"
  manual   : only fires when run by hand / by another rule

Actions (all reversible, all expressed against the config vocabulary)
  set_service    : allow/block one service for a group        -> "enable youtube"
  set_group      : allow/block ALL managed services for group -> "disable all kids devices"
  set_client     : allow/block ALL managed services for one client
  set_protection : AdGuard protection on/off (whole network)
  add_rule       : add an AdGuard custom filtering rule
  remove_rule    : remove an AdGuard custom filtering rule

These types are FROZEN. The compiler must emit exactly these shapes; the engine
dispatches on the `kind` discriminator. Keep them JSON-round-trippable (they are
persisted in SQLite as the rule's `compiled` blob).
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field

from ..models import ServiceState


# ── triggers ─────────────────────────────────────────────────────────────────
class ScheduleTrigger(BaseModel):
    kind: Literal["schedule"] = "schedule"
    cron: str                                   # 5-field cron, engine tz-aware
    describe: str = ""                          # human echo, e.g. "every day at 20:00"


SignalEdge = Literal["becomes_true", "becomes_false", "changes", "on_value"]
Comparator = Literal["is_true", "is_false", "==", "!=", ">", ">=", "<", "<="]


class SignalTrigger(BaseModel):
    kind: Literal["signal"] = "signal"
    signal: str                                 # a key from config.signals
    edge: SignalEdge = "becomes_true"
    comparator: Comparator = "is_true"
    value: Optional[Union[bool, float, str]] = None
    describe: str = ""


class ManualTrigger(BaseModel):
    kind: Literal["manual"] = "manual"
    describe: str = "run manually"


Trigger = Annotated[
    Union[ScheduleTrigger, SignalTrigger, ManualTrigger],
    Field(discriminator="kind"),
]


# ── conditions (all must hold for actions to run) ────────────────────────────
class SignalCondition(BaseModel):
    kind: Literal["signal"] = "signal"
    signal: str
    comparator: Comparator = "is_true"
    value: Optional[Union[bool, float, str]] = None


class TimeWindowCondition(BaseModel):
    kind: Literal["time_window"] = "time_window"
    after: Optional[str] = None                 # "HH:MM"
    before: Optional[str] = None                # "HH:MM"
    weekdays: Optional[list[int]] = None        # 0=Mon..6=Sun


Condition = Annotated[
    Union[SignalCondition, TimeWindowCondition],
    Field(discriminator="kind"),
]


# ── actions ──────────────────────────────────────────────────────────────────
class SetServiceAction(BaseModel):
    kind: Literal["set_service"] = "set_service"
    group: str
    service: str
    state: ServiceState


class SetGroupAction(BaseModel):
    kind: Literal["set_group"] = "set_group"
    group: str
    state: ServiceState                          # blocked = "disable all devices in group"


class SetClientAction(BaseModel):
    kind: Literal["set_client"] = "set_client"
    client: str
    state: ServiceState


class SetProtectionAction(BaseModel):
    kind: Literal["set_protection"] = "set_protection"
    enabled: bool


class AddRuleAction(BaseModel):
    kind: Literal["add_rule"] = "add_rule"
    rule: str                                    # AdGuard filtering syntax


class RemoveRuleAction(BaseModel):
    kind: Literal["remove_rule"] = "remove_rule"
    rule: str


class SetHostServiceAction(BaseModel):
    """Start or stop a service on a host (see config `managed_services`).

    GLOBAL by nature: unlike the per-group toggles this affects everyone in the house
    and ends anything mid-stream. Rules using it should say so in their wording.
    """
    kind: Literal["set_host_service"] = "set_host_service"
    service: str                                 # ManagedServiceConfig.id
    running: bool


Action = Annotated[
    Union[
        SetServiceAction, SetGroupAction, SetClientAction,
        SetProtectionAction, AddRuleAction, RemoveRuleAction,
        SetHostServiceAction,
    ],
    Field(discriminator="kind"),
]


# ── the rule ─────────────────────────────────────────────────────────────────
class CompiledRule(BaseModel):
    """The typed body. Persisted alongside the original text."""
    trigger: Trigger
    conditions: list[Condition] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    summary: str = ""                            # one-line human echo of the whole rule
    confidence: float = 1.0                      # compiler's confidence 0..1
    warnings: list[str] = Field(default_factory=list)
    # When True this rule depends on live source data (minutes, scores, completion…)
    # and is evaluated on-the-fly by the LLM evaluator against the current world state
    # rather than fired from a fixed signal. Pure time/action rules stay deterministic.
    dynamic: bool = False


class Rule(BaseModel):
    """A stored rule: the English source + its compiled form + bookkeeping."""
    id: str
    text: str                                    # the plain-English source
    enabled: bool = True
    compiled: Optional[CompiledRule] = None      # None if compilation failed
    compile_error: Optional[str] = None
    source: str = "user"                         # user | seed | api
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_fired_at: Optional[str] = None
    last_result: Optional[str] = None
    # For dynamic rules the LLM decides its OWN next wake-up as part of each
    # evaluation, from the cadence written into the rule's English ("no need to check
    # while he's at school … then hourly until 7pm"). The engine schedules a one-shot
    # job at this instant, and every other evaluation path treats a future value as
    # "not due yet". ISO-8601, UTC. None = evaluate on the safety-net tick.
    next_check_at: Optional[str] = None
    next_check_reason: Optional[str] = None
