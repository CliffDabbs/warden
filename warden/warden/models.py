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
    # AdGuard client tags that pull a device into this group automatically. ALL of
    # them must be present — [user_child, device_tablet] is "a child's tablet", not
    # "any tablet". A device type on its own therefore never implies membership.
    # Tagging a device in AdGuard is all it takes to put it under control; explicit
    # `clients:` entries always win (a named device keeps its own group).
    match_tags: list[str] = Field(default_factory=list)
    baseline: Baseline = Field(default_factory=Baseline)
    default_blocked: list[str] = Field(default_factory=list)


class ClientConfig(BaseModel):
    name: str
    group: str
    ids: list[str] = Field(default_factory=list)   # IPs or MACs (AdGuard client ids)
    subject: Optional[str] = None
    # True when membership came from an AdGuard tag rather than a `clients:` entry.
    discovered: bool = False


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


class QuickActionStep(BaseModel):
    """One flip inside a quick action.

    One of:
      * `service` (optionally narrowed to `groups`; default = every group that has it)
      * `group`   (that group's whole managed service list)
      * `host_service` + `running` (start/stop a real service — GLOBAL, everyone)
    """
    service: Optional[str] = None
    group: Optional[str] = None
    groups: list[str] = Field(default_factory=list)
    state: ServiceState = ServiceState.allowed
    host_service: Optional[str] = None
    running: Optional[bool] = None


class QuickActionConfig(BaseModel):
    """A one-tap preset shown on the Dashboard ("all streaming on, everywhere")."""
    id: str
    label: str
    description: str = ""
    icon: str = "zap"
    style: str = "default"                   # default | good | warn | danger
    steps: list[QuickActionStep] = Field(default_factory=list)
    # If set, Warden snapshots the affected switches first and restores them after
    # this many minutes — so a "movie night" unblock can't be left open overnight.
    revert_after_minutes: Optional[int] = None
    confirm: bool = False                    # ask before firing (for clamps/resets)


class HostConfig(BaseModel):
    """A machine Warden can run commands on over SSH (e.g. the NAS)."""
    name: str
    address: str
    port: int = 22
    user_env: str = ""              # env var holding the SSH username
    password_env: str = ""          # env var holding the SSH password
    known_hosts: bool = False       # False = don't verify (typical for a LAN NAS)


class ManagedServiceConfig(BaseModel):
    """A service on a host that Warden can start and stop.

    This is the escape hatch for things DNS filtering cannot touch — a media server on
    your own LAN is reachable by IP whatever the resolver says. Stopping the process is
    absolute, and correspondingly blunt: it affects EVERYONE, not one group, so it is
    deliberately modelled separately from the per-group service toggles.
    """
    id: str
    name: str
    host: str                       # HostConfig.name
    description: str = ""
    icon: str = "server"
    start: str                      # shell command to start it
    stop: str                       # shell command to stop it
    status: str = ""                # optional; prints something matched below
    status_running_match: str = "running"   # case-insensitive substring = "it's up"
    confirm: bool = True            # ask before stopping (it hits everyone)


class WardenConfig(BaseModel):
    adguard: AdGuardConfig = Field(default_factory=AdGuardConfig)
    groups: list[GroupConfig] = Field(default_factory=list)
    clients: list[ClientConfig] = Field(default_factory=list)
    services: list[ServiceConfig] = Field(default_factory=list)
    subjects: list[SubjectConfig] = Field(default_factory=list)
    sources: list[SourceConfig] = Field(default_factory=list)
    signals: list[SignalDef] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)   # seed rules (plain English)
    quick_actions: list[QuickActionConfig] = Field(default_factory=list)
    hosts: list[HostConfig] = Field(default_factory=list)
    managed_services: list[ManagedServiceConfig] = Field(default_factory=list)

    # convenience lookups -----------------------------------------------------
    def group(self, name: str) -> Optional[GroupConfig]:
        return next((g for g in self.groups if g.name == name), None)

    def service(self, sid: str) -> Optional[ServiceConfig]:
        return next((s for s in self.services if s.id == sid), None)

    def subject(self, sid: str) -> Optional[SubjectConfig]:
        return next((s for s in self.subjects if s.id == sid), None)

    def source(self, key: str) -> Optional[SourceConfig]:
        return next((s for s in self.sources if s.key == key), None)

    def quick_action(self, aid: str) -> Optional[QuickActionConfig]:
        return next((q for q in self.quick_actions if q.id == aid), None)

    def host(self, name: str) -> Optional[HostConfig]:
        return next((h for h in self.hosts if h.name == name), None)

    def managed_service(self, sid: str) -> Optional[ManagedServiceConfig]:
        return next((m for m in self.managed_services if m.id == sid), None)

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
    # A group toggle summarises several devices, and the summary can lie: blocked
    # means ALL devices block, so one exempted device (a 4h Shield unblock) makes the
    # whole card read "allowed" while the living-room TV is still blocked. `mixed`
    # marks that split and `blocked_on` names the devices still blocking, so the UI
    # can show the truth instead of the average.
    mixed: bool = False
    blocked_on: list[str] = Field(default_factory=list)


class DeviceOverride(BaseModel):
    """A standing exemption for one device from Warden's group-level writes.

    `expires_at` None means "until cancelled" — the UI's Forever option. Anything
    else is an ISO timestamp the keeper sweeps for, restoring the device's group
    state the moment it passes.
    """
    client: str
    kind: str = "unblock"
    created_at: Optional[str] = None
    expires_at: Optional[str] = None      # None = forever (until cancelled)
    actor: str = "user"


class DeviceInfo(BaseModel):
    """A device as AdGuard knows it, annotated with what Warden makes of it."""
    name: str
    ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    group: Optional[str] = None            # None = Warden doesn't manage it
    discovered: bool = False               # joined its group by tag, not by config
    blocked_services: list[str] = Field(default_factory=list)
    managed_blocked: list[str] = Field(default_factory=list)   # of Warden's services
    managed_total: int = 0
    use_global_blocked_services: bool = False
    override: Optional[DeviceOverride] = None


class ClientState(BaseModel):
    name: str
    ids: list[str]
    group: str
    subject: Optional[str] = None
    blocked_services: list[str] = Field(default_factory=list)
    online: Optional[bool] = None
    exists_in_adguard: bool = True
    discovered: bool = False        # joined via AdGuard tag, not named in config


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
    # Not a stored column — set only on the run object a live collection returns:
    # did the "state of play" actually move, or was this another identical poll?
    # SourceRegistry uses it to force a rule re-evaluation on genuinely new facts.
    state_changed: bool = False
