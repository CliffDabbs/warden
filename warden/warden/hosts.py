"""HostControl — start and stop services on machines Warden can SSH into.

Why this exists: every other switch in Warden is DNS filtering, which stops a device
*looking up a name*. It cannot stop a device that already knows an address — so a media
server on your own LAN stays reachable no matter what the resolver says. Verified on this
network: Plex clients find the LAN server by GDM broadcast and connect straight to its IP,
never asking DNS at all.

Stopping the process is the only absolute answer, and it is deliberately modelled apart
from the per-group toggles because it behaves differently:

  * It is **global**. There is no "off for the kids" — the service is up or it is down,
    for everyone in the house, and anything mid-stream stops.
  * It is **stateful on someone else's box**. If Warden is down when a rule wanted the
    service back, nothing restores it; the operator has to.

Both of those are surfaced in the UI rather than smoothed over.

Commands come from config/warden.yaml only — never from an API caller — so a request can
name a configured service id but can never supply a command to run.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from .models import HostConfig, ManagedServiceConfig

log = logging.getLogger("warden.hosts")

CONNECT_TIMEOUT = 12
COMMAND_TIMEOUT = 45


class HostControl:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self._status_cache: dict[str, tuple[float, Optional[bool], str]] = {}

    # ── availability ─────────────────────────────────────────────────────────
    def credentials(self, host: HostConfig) -> tuple[str, str]:
        return (os.getenv(host.user_env, "") if host.user_env else "",
                os.getenv(host.password_env, "") if host.password_env else "")

    def available(self, host: HostConfig) -> tuple[bool, str]:
        try:
            import asyncssh  # noqa: F401
        except Exception:
            return False, "asyncssh not installed"
        user, password = self.credentials(host)
        if not user or not password:
            return False, f"set {host.user_env} and {host.password_env} in .env"
        return True, ""

    # ── the SSH call ─────────────────────────────────────────────────────────
    async def run(self, host: HostConfig, command: str) -> tuple[int, str, str]:
        """Run one command. Returns (exit_status, stdout, stderr)."""
        import asyncssh

        ok, why = self.available(host)
        if not ok:
            raise RuntimeError(why)
        user, password = self.credentials(host)

        conn = await asyncio.wait_for(
            asyncssh.connect(
                host.address, port=host.port, username=user, password=password,
                # A LAN NAS rarely has a key in known_hosts, and failing closed here
                # would just push people to disable SSH checking globally.
                known_hosts=None if not host.known_hosts else (),
            ),
            timeout=CONNECT_TIMEOUT,
        )
        try:
            res = await asyncio.wait_for(conn.run(command, check=False),
                                         timeout=COMMAND_TIMEOUT)
            return (res.exit_status,
                    (res.stdout or "").strip(),
                    (res.stderr or "").strip())
        finally:
            conn.close()

    # ── service state ────────────────────────────────────────────────────────
    async def status(self, svc: ManagedServiceConfig) -> dict[str, Any]:
        host = self.ctx.config.host(svc.host)
        if host is None:
            return {"id": svc.id, "running": None, "detail": f"unknown host '{svc.host}'"}
        ok, why = self.available(host)
        if not ok:
            return {"id": svc.id, "running": None, "detail": why, "configured": False}
        if not svc.status:
            return {"id": svc.id, "running": None,
                    "detail": "no status command configured", "configured": True}
        try:
            code, out, err = await self.run(host, svc.status)
        except Exception as e:
            return {"id": svc.id, "running": None, "configured": True,
                    "detail": f"{type(e).__name__}: {e}"}
        blob = f"{out}\n{err}".lower()
        running = svc.status_running_match.lower() in blob
        return {"id": svc.id, "running": running, "configured": True,
                "detail": (out or err or f"exit {code}")[:200]}

    async def set_running(self, svc: ManagedServiceConfig, running: bool) -> dict[str, Any]:
        host = self.ctx.config.host(svc.host)
        if host is None:
            raise RuntimeError(f"unknown host '{svc.host}'")
        command = svc.start if running else svc.stop
        code, out, err = await self.run(host, command)
        detail = (out or err or f"exit {code}")[:300]
        ok = code == 0
        await self.ctx.audit(
            "user", "host_service", f"{svc.id}={'running' if running else 'stopped'}",
            detail, ok=ok)
        self._status_cache.pop(svc.id, None)
        if not ok:
            log.warning("%s on %s exited %s: %s", command, host.name, code, detail)
        return {"id": svc.id, "requested": "running" if running else "stopped",
                "ok": ok, "detail": detail}

    async def list_services(self) -> list[dict[str, Any]]:
        """Every managed service with its live state (concurrently probed)."""
        svcs = self.ctx.config.managed_services
        if not svcs:
            return []
        states = await asyncio.gather(*(self.status(s) for s in svcs),
                                      return_exceptions=True)
        out: list[dict[str, Any]] = []
        for svc, st in zip(svcs, states):
            if isinstance(st, Exception):
                st = {"running": None, "detail": f"{type(st).__name__}: {st}"}
            out.append({
                "id": svc.id, "name": svc.name, "description": svc.description,
                "icon": svc.icon, "host": svc.host, "confirm": svc.confirm,
                "running": st.get("running"), "detail": st.get("detail", ""),
                "configured": st.get("configured", True),
            })
        return out

    # ── first-run helper ─────────────────────────────────────────────────────
    async def discover(self, host: HostConfig) -> dict[str, Any]:
        """Work out how this box wants services controlled.

        A QNAP may run Plex as a QPKG (`qpkg_cli`) or as a Container Station container
        (`docker`), and the right command differs. Rather than guess, look.
        """
        probes = {
            "uname": "uname -a",
            "qpkg_cli": "command -v qpkg_cli || ls /sbin/qpkg_cli 2>/dev/null",
            "qpkg_list": "/sbin/qpkg_cli --list 2>/dev/null | head -40",
            "docker": "command -v docker",
            "docker_ps": "docker ps --format '{{.Names}}\t{{.Image}}' 2>/dev/null | head -20",
            "systemctl": "command -v systemctl",
            "initd": "ls /etc/init.d 2>/dev/null | head -40",
        }
        out: dict[str, Any] = {}
        for label, cmd in probes.items():
            try:
                code, so, se = await self.run(host, cmd)
                out[label] = {"exit": code, "out": so[:1500], "err": se[:200]}
            except Exception as e:
                out[label] = {"error": f"{type(e).__name__}: {e}"}
                break          # connection problem: no point running the rest
        return out
