"""OverrideKeeper — expires timed device unblocks and puts the blocks back.

A parent unblocking a device for two hours creates a row in `overrides`. Two things
then have to be true, or the feature is a lie:

  * nothing re-blocks the device while the override stands — handled in
    AdGuardService._apply, which skips exempt clients on every group write;
  * the device DOES get re-blocked the moment the clock runs out — handled here.

So this is a plain 60s sweep: find overrides whose expires_at has passed, restore the
device's group state, drop the row, audit it, push the UI. A "forever" override has a
NULL expires_at and is never swept — only an explicit cancel removes it.

The sweep is deliberately dumb and idempotent. If Warden is down when an override
expires, the very next sweep after boot catches it; nothing is lost by missing a tick.
"""
from __future__ import annotations

import asyncio
import logging

from .models import ServiceState, utcnow

log = logging.getLogger("warden.overrides")

SWEEP_SECONDS = 60


class OverrideKeeper:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            log.info("override keeper started (sweep every %ds)", SWEEP_SECONDS)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception as e:            # a bad sweep must not kill the loop
                log.warning("override sweep failed: %s", e)
            await asyncio.sleep(SWEEP_SECONDS)

    async def sweep(self) -> int:
        """Restore every device whose override has expired. Returns how many."""
        due = self.ctx.db.expired_overrides(utcnow().isoformat())
        if not due:
            return 0
        for o in due:
            # Drop the row FIRST: while it stands the device is exempt, and the
            # re-block below goes through the same service the exemption guards.
            self.ctx.db.delete_override(o.client)
            try:
                await self.ctx.adguard.set_device_services(o.client, ServiceState.blocked)
                await self.ctx.audit("system", "override.expired", o.client,
                                     "unblock expired — services re-blocked")
            except Exception as e:
                await self.ctx.audit("system", "override.expired", o.client,
                                     f"expired but re-block failed: {e}", ok=False)
        await self.ctx.after_change("override.expired")
        return len(due)
