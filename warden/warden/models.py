"""Domain models — the shared vocabulary every module speaks.

Split into three groups:
  * config models   — parsed from config/warden.yaml (WardenConfig and friends)
  * exchange models — Item (adapter output) and Signal (bus value); the contract
                      lingua franca, mirroring docs/sourceadapterCONTRACT.md §3
  * view models     — runtime snapshots the API/UI render (StateSnapshot etc.)

These types are FROZEN: parallel modules import them and must not redefine them.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── enums ────────────────────────────────────────────────────────────────────
class ServiceState(str, Enum):
    """Runtime state of a service for a group/client. ON = allowed, OFF = blocked."""
    allowed = "allowed"
    blocked = "blocked"


class SignalType(str, Enum):
    bool = "bool"
    number = "number"
    string = "string"


# ── config models (config/warden.yaml) ──────────────────────────────────────
class Baseline(BaseModel):
    safe_search: bool = False
    parental: bool = False
    safebrowsing: bool = False


class GroupConfig(BaseModel):
    name: str
    tag: Optional[str] = None
    baseline: Baseline = Field(default_factory=Baseline)
    default_blocked: list[str] = Field(default_factory=list)


class ClientConfig(BaseModel):
    name: str
    group: str
    ids: list[str] = Field(default_factory=list)   # IPs or MACs (AdGuard client ids)
    subject: Optional[str] = None


class ServiceConfig(BaseModel):
    id: str                       # valid AdGuard blocked-service id
    name: str
    icon: str = "globe"
    groups: list[str] = Field(default_factory=list)


class SubjectContext(BaseModel):
    label: str
    effective_from: Optional[str] = None   # ISO date; date-driven profile rollover
    effective_to: Optional[str] = None


class SubjectConfig(BaseModel):
    id: str
    name: str
    contexts: list[SubjectContext] = Field(default_factory=list)


class SourceConfig(BaseModel):
    key: str                      # the map key in sources: (e.g. "atom")
    adapter: str                  # adapter package name under warden/adapters/
    display_name: str = ""
    enabled: bool = True
    schedule: Optional[str] = None            # cron expression
    subject: Optional[str] = None
    secrets: dict[str, str] = Field(default_factory=dict)   # logical -> ENV var name
    options: dict[str, Any] = Field(default_factory=dict)


class SignalDef(BaseModel):
    key: str
    type: SignalType = SignalType.bool
    source: Optional[str] = None
    subject: Optional[str] = None
    describe: str = ""


class AdGuardConfig(BaseModel):
    url: str = "http://10.7.11.29"


class WardenConfig(BaseModel):
    adguard: AdGuardConfig = Field(default_factory=AdGuardConfig)
    groups: list[GroupConfig] = Field(default_factory=list)
    clients: list[ClientConfig] = Field(default_factory=list)
    services: list[ServiceConfig] = Field(default_factory=list)
    subjects: list[SubjectConfig] = Field(default_factory=list)
    sources: list[SourceConfig] = Field(default_factory=list)
    signals: list[SignalDef] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)   # seed rules (plain English)

    # convenience lookups -----------------------------------------------------
    def group(self, name: str) -> Optional[GroupConfig]:
        return next((g for g in self.groups if g.name == name), None)

    def service(self, sid: str) -> Optional[ServiceConfig]:
        return next((s for s in self.services if s.id == sid), None)

    def subject(self, sid: str) -> Optional[SubjectConfig]:
        return next((s for s in self.subjects if s.id == sid), None)

    def source(self, key: str) -> Optional[SourceConfig]:
        return next((s for s in self.sources if s.key == key), None)

    def clients_in_group(self, group: str) -> list[ClientConfig]:
        return [c for c in self.clients if c.group == group]

    def services_for_group(self, group: str) -> list[ServiceConfig]:
        return [s for s in self.services if group in s.groups]


# ── exchange models (adapter output + signal bus) ────────────────────────────
class Attachment(BaseModel):
    kind: str = "file"
    url: Optional[str] = None
    mime: Optional[str] = None
    name: Optional[str] = None


class Item(BaseModel):
    """Normalised item from a source adapter (docs/sourceadapterCONTRACT.md §3)."""
    source_id: str
    account_id: str = "default"
    subject_ids: list[str] = Field(default_factory=list)
    external_id: str                              # STABLE, unique — drives dedupe
    kind: str = "other"                           # message|calendar_event|form|post|progress|other
    title: str = ""
    body_text: str = ""
    occurred_at: Optional[datetime] = None
    due_at: Optional[datetime] = None
    audience_tags: list[str] = Field(default_factory=list)
    url: Optional[str] = None
    attachments: list[Attachment] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)
    fetched_at: datetime = Field(default_factory=utcnow)


class Signal(BaseModel):
    """A single derived fact the rules engine can react to."""
    key: str
    value: Any                                    # bool | number | str
    type: SignalType = SignalType.bool
    source: Optional[str] = None
    subject: Optional[str] = None
    at: datetime = Field(default_factory=utcnow)
    meta: dict[str, Any] = Field(default_factory=dict)


# ── view models (what the API/UI render) ─────────────────────────────────────
class ServiceToggle(BaseModel):
    id: str
    name: str
    icon: str
    state: ServiceState


class ClientState(BaseModel):
    name: str
    ids: list[str]
    group: str
    subject: Optional[str] = None
    blocked_services: list[str] = Field(default_factory=list)
    online: Optional[bool] = None
    exists_in_adguard: bool = True


class GroupState(BaseModel):
    name: str
    tag: Optional[str] = None
    services: list[ServiceToggle] = Field(default_factory=list)
    clients: list[ClientState] = Field(default_factory=list)
    all_blocked: bool = False                     # every managed service blocked?


class AdGuardStatus(BaseModel):
    reachable: bool = False
    mode: str = "fake"                            # live | fake
    version: Optional[str] = None
    protection_enabled: Optional[bool] = None
    url: Optional[str] = None
    detail: str = ""


class StateSnapshot(BaseModel):
    """Everything the dashboard needs in one payload."""
    adguard: AdGuardStatus
    groups: list[GroupState] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utcnow)


class AuditEntry(BaseModel):
    id: Optional[int] = None
    at: datetime = Field(default_factory=utcnow)
    actor: str = "system"                         # system | user | rule:<id> | source:<key>
    action: str = ""                              # set_service | block_group | add_rule | source_run ...
    target: str = ""
    detail: str = ""
    ok: bool = True


class SourceRun(BaseModel):
    id: Optional[int] = None
    source: str
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    ok: bool = False
    item_count: int = 0
    signal_count: int = 0
    detail: str = ""
