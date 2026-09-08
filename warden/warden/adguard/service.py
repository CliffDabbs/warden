"""AdGuardService — the one class the rest of Warden talks to for network control.

Backend-agnostic: it drives either `AdGuardClient` (live REST) or `FakeAdGuard`
(in-memory) through an identical low-level surface (the `Backend` protocol below),
so behaviour is the same online and offline. `build_adguard` constructs it;
`connect()` picks the backend (auto-probe under ADGUARD_MODE=auto).

The service is PURE: it changes AdGuard state and returns view models, but never
audits or pushes UI events — callers (API routes, rules engine) wrap each mutation
with `ctx.audit(...)` + `ctx.after_change(...)`.

Canonical service-state model (INTERFACES.md):
  * a service is *blocked* for a group when its id is in `blocked_services` of the
    group's clients; *allowed* otherwise.
  * set_service adds/removes one service id across the group's clients.
  * set_group adds/removes ALL managed services (config.services_for_group).
  * in a snapshot, a group's toggle is blocked iff every existing client blocks it
    (no clients ⇒ fall back to the group's config default_blocked); all_blocked iff
    every managed service is blocked.
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Protocol

from ..config import Settings
from ..models import (
    AdGuardStatus, ClientConfig, ClientState, DeviceInfo, GroupState, ServiceState,
    ServiceToggle, StateSnapshot, WardenConfig,
)
from .client import AdGuardClient
from .fake import FakeAdGuard


class Backend(Protocol):
    """The low-level surface shared by AdGuardClient and FakeAdGuard."""
    async def get_status(self) -> dict[str, Any]: ...
    async def get_clients(self) -> list[dict[str, Any]]: ...
    async def get_client(self, name: str) -> Optional[dict[str, Any]]: ...
    async def get_user_rules(self) -> tuple[list[str], bool]: ...
    async def set_client_services(self, name: str, services: list[str]) -> None: ...
    async def add_client(self, client: dict[str, Any]) -> None: ...
    async def set_protection(self, enabled: bool) -> None: ...
    async def add_user_rule(self, rule: str) -> None: ...
    async def remove_user_rule(self, rule: str) -> None: ...
    async def close(self) -> None: ...


def build_adguard(settings: Settings, config: WardenConfig) -> "AdGuardService":
    return AdGuardService(settings, config)


class AdGuardService:
    def __init__(self, settings: Settings, config: WardenConfig) -> None:
        self.settings = settings
        self.config = config
        self.url = settings.adguard_url
        self.mode = "fake"
        # default to fake so every method is safe to call before connect();
        # connect() may swap in the live backend.
        self._fake = FakeAdGuard(config)
        self._live: Optional[AdGuardClient] = None
        self._backend: Backend = self._fake
        self._connected = False
        # Names currently exempt from group writes. main.py points this at the DB;
        # a callable (not a set) so the service stays free of storage concerns and
        # every write sees the live answer rather than a stale copy.
        self.active_overrides: Callable[[], set[str]] = lambda: set()

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def connect(self) -> None:
        """Pick live vs fake. Idempotent — safe to call repeatedly."""
        if self._connected:
            return
        mode = self.settings.adguard_mode
        use_live = mode == "live" or (mode == "auto" and await self._probe_live())
        if use_live:
            if self._live is None:                 # explicit "live" mode: no probe ran
                self._live = AdGuardClient(self.settings)
            self._backend = self._live
            self.mode = "live"
        else:
            self._backend = self._fake
            self.mode = "fake"
        self._connected = True

    async def _probe_live(self) -> bool:
        """Live only if creds are present AND GET /control/status succeeds."""
        if not (self.settings.adguard_user and self.settings.adguard_pass):
            return False
        client = AdGuardClient(self.settings)
        try:
            await client.get_status()
            self._live = client                    # reuse the proven connection
            return True
        except Exception:
            await client.close()
            return False

    async def close(self) -> None:
        if self._live is not None:
            await self._live.close()
        self._connected = False

    # ── membership ───────────────────────────────────────────────────────────
    def _resolve_clients(self, raw_clients: list[dict[str, Any]]) -> list[ClientConfig]:
        """Every managed device: those named in `clients:`, then tag-discovered ones.

        A device named in config keeps its configured group. Otherwise it joins a group
        whose `match_tags` it carries IN FULL — every listed tag must be present. So
        [user_child, device_tablet] means "a tablet belonging to a child", NOT "any
        tablet, or any child's device": a device type alone never implies a group.

        Groups are tried most-specific-first (most tags wins; config order breaks ties),
        so a narrow group never loses a device to a broader one. A device joins at most
        one group, so it can never receive conflicting writes.
        """
        candidates = sorted(
            (g for g in self.config.groups if g.match_tags),
            key=lambda g: -len(g.match_tags),      # stable: config order breaks ties
        )
        out = list(self.config.clients)
        claimed = {c.name for c in out}
        for raw in raw_clients:
            name = raw.get("name")
            if not name or name in claimed:        # explicit config entry wins
                continue
            tags = set(raw.get("tags") or [])
            if not tags:
                continue
            g = next((g for g in candidates if set(g.match_tags) <= tags), None)
            if g is None:
                continue
            claimed.add(name)
            out.append(ClientConfig(
                name=name, group=g.name,
                ids=list(raw.get("ids") or []), discovered=True,
            ))
        return out

    def _members(self, group: str, resolved: list[ClientConfig]) -> list[ClientConfig]:
        return [c for c in resolved if c.group == group]

    async def resolve_clients(self) -> list[ClientConfig]:
        """Public: membership as it stands in AdGuard right now (used for validation)."""
        return self._resolve_clients(await self._backend.get_clients())

    async def list_devices(self, overrides: Optional[dict[str, Any]] = None) -> list[DeviceInfo]:
        """EVERY device AdGuard knows, not just the managed ones.

        The override page needs the full picture — an unmanaged device is still worth
        showing so it's obvious why nothing is being blocked on it.
        """
        raws = await self._backend.get_clients()
        group_of = {c.name: c for c in self._resolve_clients(raws)}
        out: list[DeviceInfo] = []
        for raw in raws:
            name = raw.get("name")
            if not name:
                continue
            cc = group_of.get(name)
            blocked = list(raw.get("blocked_services") or [])
            managed = [s.id for s in self.config.services_for_group(cc.group)] if cc else []
            out.append(DeviceInfo(
                name=name,
                ids=list(raw.get("ids") or []),
                tags=list(raw.get("tags") or []),
                group=cc.group if cc else None,
                discovered=bool(cc and cc.discovered),
                blocked_services=blocked,
                managed_blocked=[s for s in managed if s in blocked],
                managed_total=len(managed),
                use_global_blocked_services=bool(raw.get("use_global_blocked_services")),
                override=(overrides or {}).get(name),
            ))
        out.sort(key=lambda d: (d.group is None, d.group or "", d.name.lower()))
        return out

    async def set_device_services(self, name: str, state: ServiceState) -> None:
        """Block/allow a device's managed services, ignoring any override.

        `set_client` refuses unknown names and is the path rules take; this is the
        override page's direct lever, used when the exemption is being created or
        lifted and so must not be blocked by itself.
        """
        raws = await self._backend.get_clients()
        cc = next((c for c in self._resolve_clients(raws) if c.name == name), None)
        raw = next((r for r in raws if r.get("name") == name), None)
        if raw is None:
            raise ValueError(f"client not present in AdGuard: {name!r}")
        if cc is None:
            raise ValueError(f"device is not in any Warden group: {name!r}")
        svc_ids = {s.id for s in self.config.services_for_group(cc.group)}
        blocked = set(raw.get("blocked_services") or [])
        new = (blocked | svc_ids) if state is ServiceState.blocked else (blocked - svc_ids)
        if new != blocked:
            await self._backend.set_client_services(name, sorted(new))

    # ── reads ────────────────────────────────────────────────────────────────
    async def status(self) -> AdGuardStatus:
        try:
            raw = await self._backend.get_status()
        except Exception as e:                     # unreachable box: report, don't crash
            return AdGuardStatus(reachable=False, mode=self.mode, url=self.url, detail=str(e))
        return AdGuardStatus(
            reachable=True, mode=self.mode,
            version=raw.get("version"),
            protection_enabled=raw.get("protection_enabled"),
            url=self.url, detail="",
        )

    async def list_clients(self) -> list[ClientState]:
        """Managed devices (config-named + tag-matched), flagged if absent from AdGuard."""
        raws = await self._backend.get_clients()
        by_name = {c.get("name"): c for c in raws}
        out: list[ClientState] = []
        for cc in self._resolve_clients(raws):
            raw = by_name.get(cc.name)
            out.append(ClientState(
                name=cc.name, ids=cc.ids, group=cc.group, subject=cc.subject,
                blocked_services=list(raw.get("blocked_services") or []) if raw else [],
                exists_in_adguard=raw is not None,
                discovered=cc.discovered,
            ))
        return out

    async def snapshot(self) -> StateSnapshot:
        status = await self.status()
        raws = await self._backend.get_clients()
        by_name = {c.get("name"): c for c in raws}
        resolved = self._resolve_clients(raws)
        groups: list[GroupState] = []
        for g in self.config.groups:
            clients: list[ClientState] = []
            for cc in self._members(g.name, resolved):
                raw = by_name.get(cc.name)
                clients.append(ClientState(
                    name=cc.name, ids=cc.ids, group=g.name, subject=cc.subject,
                    blocked_services=list(raw.get("blocked_services") or []) if raw else [],
                    exists_in_adguard=raw is not None,
                    discovered=cc.discovered,
                ))
            existing = [c for c in clients if c.exists_in_adguard]
            toggles: list[ServiceToggle] = []
            for s in self.config.services_for_group(g.name):
                blocked_on: list[str] = []
                if existing:
                    blocked_on = [c.name for c in existing if s.id in c.blocked_services]
                    blocked = len(blocked_on) == len(existing)
                else:                              # no provisioned device: config intent
                    blocked = s.id in g.default_blocked
                toggles.append(ServiceToggle(
                    id=s.id, name=s.name, icon=s.icon,
                    # state keeps its historical meaning (blocked iff ALL block) so the
                    # evaluator's idempotence checks are unchanged; mixed carries the split
                    state=ServiceState.blocked if blocked else ServiceState.allowed,
                    mixed=bool(blocked_on) and not blocked,
                    blocked_on=blocked_on if not blocked else [],
                ))
            groups.append(GroupState(
                name=g.name, tag=g.tag, services=toggles, clients=clients,
                all_blocked=bool(toggles) and all(t.state is ServiceState.blocked for t in toggles),
            ))
        return StateSnapshot(adguard=status, groups=groups)

    # ── mutations ────────────────────────────────────────────────────────────
    async def set_service(self, group: str, service: str, state: ServiceState) -> None:
        """Block/allow one service across every client in a group."""
        self._require_group(group)
        if self.config.service(service) is None:
            raise ValueError(f"unknown service: {service!r}")
        await self._apply(group, {service}, state)

    async def set_group(self, group: str, state: ServiceState) -> None:
        """Block/allow ALL managed services for a group (the bedtime switch)."""
        self._require_group(group)
        svc_ids = {s.id for s in self.config.services_for_group(group)}
        await self._apply(group, svc_ids, state)

    async def set_client(self, client: str, state: ServiceState) -> None:
        """Block/allow all of a single device's managed services."""
        raws = await self._backend.get_clients()
        cc = next((c for c in self._resolve_clients(raws) if c.name == client), None)
        if cc is None:
            raise ValueError(f"unknown client: {client!r}")
        svc_ids = {s.id for s in self.config.services_for_group(cc.group)}
        cur = await self._backend.get_client(client)
        if cur is None:                            # device not provisioned in AdGuard yet
            raise ValueError(f"client not present in AdGuard: {client!r}")
        blocked = set(cur.get("blocked_services") or [])
        new = (blocked | svc_ids) if state is ServiceState.blocked else (blocked - svc_ids)
        if new != blocked:
            await self._backend.set_client_services(client, sorted(new))

    async def set_protection(self, enabled: bool) -> None:
        await self._backend.set_protection(enabled)

    async def _apply(self, group: str, svc_ids: set[str], state: ServiceState) -> None:
        """Add/remove `svc_ids` in each existing group client's blocked_services."""
        raws = await self._backend.get_clients()
        by_name = {c.get("name"): c for c in raws}
        exempt = self.active_overrides()
        for cc in self._members(group, self._resolve_clients(raws)):
            raw = by_name.get(cc.name)
            if raw is None:                        # skip devices not yet in AdGuard
                continue
            if cc.name in exempt:                  # manual unblock outranks the group
                continue
            blocked = set(raw.get("blocked_services") or [])
            new = (blocked | svc_ids) if state is ServiceState.blocked else (blocked - svc_ids)
            if new != blocked:                     # only write when something changes
                await self._backend.set_client_services(cc.name, sorted(new))

    def _require_group(self, group: str) -> None:
        if self.config.group(group) is None:
            raise ValueError(f"unknown group: {group!r}")

    # ── AdGuard user-rules (custom filtering rules) ──────────────────────────
    async def list_rules(self) -> list[str]:
        rules, _ = await self._backend.get_user_rules()
        return rules

    async def add_rule(self, rule: str) -> None:
        await self._backend.add_user_rule(rule)

    async def remove_rule(self, rule: str) -> None:
        await self._backend.remove_user_rule(rule)
