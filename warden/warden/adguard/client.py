"""AdGuardClient — a thin async httpx wrapper over the AdGuard Home REST API.

Only the endpoints Warden needs (verified live on v0.107.78, see INTERFACES.md):
  GET  /control/status                 → version / protection / running
  GET  /control/clients                → {clients:[...], auto_clients:[...]}
  POST /control/clients/update         → overwrites a whole client (GET-merge-PUT)
  POST /control/clients/add            → create a client
  GET  /control/filtering/status       → {enabled, user_rules:[...]}
  POST /control/filtering/set_rules    → replaces the whole user-rules list
  POST /control/protection             → {enabled:bool}
  GET  /control/blocked_services/all   → catalogue of the 136 blockable services

The two mutating "merge" helpers live here: AdGuard overwrites the whole object on
write, so we GET, change only the field we care about, and PUT the rest back intact.
`fake.py` mirrors this exact method surface so `service.py` is backend-agnostic.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

from ..config import Settings


class AdGuardClient:
    """Live backend. Lazily opens one AsyncClient; basic-auth from Settings."""

    def __init__(self, settings: Settings) -> None:
        self._base = settings.adguard_url.rstrip("/")
        # AdGuard uses HTTP Basic auth; skip it entirely if no user is configured.
        self._auth: Optional[tuple[str, str]] = (
            (settings.adguard_user, settings.adguard_pass) if settings.adguard_user else None
        )
        # short connect timeout so an unreachable box doesn't stall boot/probe.
        self._timeout = httpx.Timeout(10.0, connect=5.0)
        self._http: Optional[httpx.AsyncClient] = None

    # ── connection lifecycle ─────────────────────────────────────────────────
    def _c(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=self._base, auth=self._auth, timeout=self._timeout)
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ── reads ────────────────────────────────────────────────────────────────
    async def get_status(self) -> dict[str, Any]:
        r = await self._c().get("/control/status")
        r.raise_for_status()
        return r.json()

    async def get_clients(self) -> list[dict[str, Any]]:
        """The configured clients array (auto_clients are DHCP guesses; ignored)."""
        r = await self._c().get("/control/clients")
        r.raise_for_status()
        return list(r.json().get("clients") or [])

    async def get_client(self, name: str) -> Optional[dict[str, Any]]:
        return next((c for c in await self.get_clients() if c.get("name") == name), None)

    async def get_user_rules(self) -> tuple[list[str], bool]:
        r = await self._c().get("/control/filtering/status")
        r.raise_for_status()
        data = r.json()
        return list(data.get("user_rules") or []), bool(data.get("enabled", True))

    async def get_blocked_services_catalog(self) -> list[dict[str, Any]]:
        r = await self._c().get("/control/blocked_services/all")
        r.raise_for_status()
        return list(r.json().get("blocked_services") or [])

    # ── writes (GET-merge-PUT where AdGuard overwrites the whole object) ──────
    async def set_client_services(self, name: str, services: list[str]) -> None:
        """Replace a client's blocked_services, preserving every other field."""
        cur = await self.get_client(name)
        if cur is None:
            raise ValueError(f"AdGuard client not found: {name!r}")
        data = dict(cur)
        data["blocked_services"] = list(services)
        # per-service blocking only takes effect when the client isn't deferring to
        # the global list, so make that explicit on every write.
        data["use_global_blocked_services"] = False
        r = await self._c().post("/control/clients/update", json={"name": name, "data": data})
        r.raise_for_status()

    async def add_client(self, client: dict[str, Any]) -> None:
        r = await self._c().post("/control/clients/add", json=client)
        r.raise_for_status()

    async def set_protection(self, enabled: bool) -> None:
        r = await self._c().post("/control/protection", json={"enabled": bool(enabled)})
        r.raise_for_status()

    async def set_user_rules(self, rules: list[str]) -> None:
        r = await self._c().post("/control/filtering/set_rules", json={"rules": list(rules)})
        r.raise_for_status()

    async def add_user_rule(self, rule: str) -> None:
        rules, _ = await self.get_user_rules()
        if rule not in rules:
            await self.set_user_rules([*rules, rule])

    async def remove_user_rule(self, rule: str) -> None:
        rules, _ = await self.get_user_rules()
        if rule in rules:
            await self.set_user_rules([r for r in rules if r != rule])
