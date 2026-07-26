"""SignalBus — the in-memory pub/sub for derived facts, persisted to SQLite.

Adapters (and manual/API callers) `emit()` Signals. The bus stores the latest value
per key plus an append-only history, computes whether the value *changed* and which
*edge* it crossed, and notifies subscribers (the rules engine). Everything is keyed by
the signal `key` from config.signals.

FROZEN interface — the engine subscribes here and adapters emit here.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Optional

from ..models import Signal


# subscriber(signal, changed, previous) -> optional awaitable
Subscriber = Callable[[Signal, bool, Optional[Signal]], Optional[Awaitable[None]]]


def _edge(prev: Optional[Signal], cur: Signal) -> str:
    """Classify the transition for signal-trigger matching."""
    if prev is None:
        return "init"
    if prev.value == cur.value:
        return "same"
    if isinstance(cur.value, bool) or isinstance(prev.value, bool):
        if cur.value and not prev.value:
            return "becomes_true"
        if prev.value and not cur.value:
            return "becomes_false"
    return "changes"


class SignalBus:
    def __init__(self, db) -> None:
        self._db = db
        self._latest: dict[str, Signal] = {}
        self._subs: list[Subscriber] = []
        # warm cache from persisted latest values
        for s in self._db.latest_signals():
            self._latest[s.key] = s

    def subscribe(self, cb: Subscriber) -> None:
        self._subs.append(cb)

    def get(self, key: str) -> Optional[Signal]:
        return self._latest.get(key)

    def all(self) -> list[Signal]:
        return list(self._latest.values())

    def history(self, key: str, limit: int = 50) -> list[Signal]:
        return self._db.signal_history(key, limit)

    async def emit(self, signal: Signal) -> tuple[bool, str]:
        """Store the signal, persist it, and fan out to subscribers.

        Returns (changed, edge). `edge` is one of init|same|becomes_true|
        becomes_false|changes — used by SignalTrigger matching in the engine.
        """
        prev = self._latest.get(signal.key)
        edge = _edge(prev, signal)
        changed = edge not in ("same",)
        self._latest[signal.key] = signal
        self._db.record_signal(signal)
        for cb in list(self._subs):
            res = cb(signal, changed, prev)
            if res is not None:
                await res
        return changed, edge
