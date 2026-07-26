"""FakeAdGuard — an in-memory stand-in exposing the SAME low-level surface as
`AdGuardClient`, so `service.py` runs identically offline (the demo/default path).

Seeded from config: one client per `config.clients`, its blocked_services set to the
group's `default_blocked`, its baseline flags from the group's `baseline`. Holds a
protection flag and a user-rules list. Every read returns copies so callers can't
mutate our state by accident — same isolation you'd get over HTTP.
"""
from __future__ import annotations

from typing import Any, Optional

from ..models import GroupConfig, WardenConfig


class FakeAdGuard:
    def __init__(self, config: WardenConfig) -> None:
        self._config = config
        self._clients: dict[str, dict[str, Any]] = {}
        self._protection = True
        self._rules: list[str] = []
        self._filtering_enabled = True
        for cc in config.clients:
            g = config.group(cc.group)
            default = list(g.default_blocked) if g else []
            self._clients[cc.name] = self._make_client(cc.name, cc.ids, g, default)

    @staticmethod
    def _make_client(name: str, ids: list[str], g: Optional[GroupConfig],
                     blocked: list[str]) -> dict[str, Any]:
        """A client object shaped like AdGuard's real one (the fields we touch)."""
        b = g.baseline if g else None
        return {
            "name": name,
            "ids": list(ids),
            "tags": [g.tag] if (g and g.tag) else [],
            "blocked_services": list(blocked),
            "use_global_settings": False,
            "use_global_blocked_services": False,
            "filtering_enabled": True,
            "parental_enabled": bool(b.parental) if b else False,
            "safebrowsing_enabled": bool(b.safebrowsing) if b else False,
            "safesearch_enabled": bool(b.safe_search) if b else False,
        }

    async def close(self) -> None:
        return None

    # ── reads ────────────────────────────────────────────────────────────────
    async def get_status(self) -> dict[str, Any]:
        return {"version": "fake-adguard", "protection_enabled": self._protection, "running": True}

    async def get_clients(self) -> list[dict[str, Any]]:
        return [dict(c) for c in self._clients.values()]

    async def get_client(self, name: str) -> Optional[dict[str, Any]]:
        c = self._clients.get(name)
        return dict(c) if c is not None else None

    async def get_user_rules(self) -> tuple[list[str], bool]:
        return list(self._rules), self._filtering_enabled

    async def get_blocked_services_catalog(self) -> list[dict[str, Any]]:
        return [{"id": s.id, "name": s.name} for s in self._config.services]

    # ── writes ───────────────────────────────────────────────────────────────
    async def set_client_services(self, name: str, services: list[str]) -> None:
        c = self._clients.get(name)
        if c is None:
            raise ValueError(f"AdGuard client not found: {name!r}")
        c["blocked_services"] = list(services)
        c["use_global_blocked_services"] = False

    async def add_client(self, client: dict[str, Any]) -> None:
        self._clients[client["name"]] = dict(client)

    async def set_protection(self, enabled: bool) -> None:
        self._protection = bool(enabled)

    async def set_user_rules(self, rules: list[str]) -> None:
        self._rules = list(rules)

    async def add_user_rule(self, rule: str) -> None:
        if rule not in self._rules:
            self._rules.append(rule)

    async def remove_user_rule(self, rule: str) -> None:
        self._rules = [r for r in self._rules if r != rule]
