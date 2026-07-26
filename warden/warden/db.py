"""SQLite state store — rules, signals, audit, source runs, items, cursors.

Single-file, dependency-free (stdlib sqlite3). Thread-safe via a write lock because
APScheduler fires jobs on worker threads while FastAPI runs on the event loop.
Pydantic models are stored as JSON text; timestamps as ISO-8601 strings.

FROZEN interface — bus.py, the engine, adapters registry and the API all call these.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .models import AuditEntry, Signal, SignalType, SourceRun, Item, utcnow
from .rules.schema import Rule

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    compiled TEXT,
    compile_error TEXT,
    source TEXT DEFAULT 'user',
    created_at TEXT,
    updated_at TEXT,
    last_fired_at TEXT,
    last_result TEXT
);
CREATE TABLE IF NOT EXISTS signals_latest (
    key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS signal_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    payload TEXT NOT NULL,
    at TEXT
);
CREATE INDEX IF NOT EXISTS ix_sighist_key ON signal_history(key, id DESC);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT, actor TEXT, action TEXT, target TEXT, detail TEXT, ok INTEGER
);
CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT, started_at TEXT, finished_at TEXT,
    ok INTEGER, item_count INTEGER, signal_count INTEGER, detail TEXT
);
CREATE TABLE IF NOT EXISTS items (
    external_id TEXT PRIMARY KEY,
    source_id TEXT, payload TEXT, fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_items_source ON items(source_id, fetched_at DESC);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS state_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL, at TEXT, hash TEXT, payload TEXT
);
CREATE INDEX IF NOT EXISTS ix_statehist ON state_history(source, id DESC);
"""


class DB:
    def __init__(self, path: str = "warden.db") -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True) if Path(path).parent != Path("") else None
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ── rules ────────────────────────────────────────────────────────────────
    def upsert_rule(self, rule: Rule) -> None:
        now = utcnow().isoformat()
        rule.created_at = rule.created_at or now
        rule.updated_at = now
        compiled = rule.compiled.model_dump_json() if rule.compiled else None
        with self._lock:
            self._conn.execute(
                """INSERT INTO rules (id,text,enabled,compiled,compile_error,source,
                       created_at,updated_at,last_fired_at,last_result)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       text=excluded.text, enabled=excluded.enabled,
                       compiled=excluded.compiled, compile_error=excluded.compile_error,
                       source=excluded.source, updated_at=excluded.updated_at,
                       last_fired_at=excluded.last_fired_at, last_result=excluded.last_result""",
                (rule.id, rule.text, int(rule.enabled), compiled, rule.compile_error,
                 rule.source, rule.created_at, rule.updated_at,
                 rule.last_fired_at, rule.last_result),
            )
            self._conn.commit()

    def _row_to_rule(self, row: sqlite3.Row) -> Rule:
        from .rules.schema import CompiledRule
        compiled = None
        if row["compiled"]:
            compiled = CompiledRule.model_validate_json(row["compiled"])
        return Rule(
            id=row["id"], text=row["text"], enabled=bool(row["enabled"]),
            compiled=compiled, compile_error=row["compile_error"], source=row["source"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            last_fired_at=row["last_fired_at"], last_result=row["last_result"],
        )

    def get_rule(self, rid: str) -> Optional[Rule]:
        row = self._conn.execute("SELECT * FROM rules WHERE id=?", (rid,)).fetchone()
        return self._row_to_rule(row) if row else None

    def list_rules(self) -> list[Rule]:
        rows = self._conn.execute("SELECT * FROM rules ORDER BY created_at").fetchall()
        return [self._row_to_rule(r) for r in rows]

    def delete_rule(self, rid: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM rules WHERE id=?", (rid,))
            self._conn.commit()

    def touch_rule(self, rid: str, last_fired_at: str, last_result: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE rules SET last_fired_at=?, last_result=? WHERE id=?",
                (last_fired_at, last_result, rid),
            )
            self._conn.commit()

    # ── signals ──────────────────────────────────────────────────────────────
    def record_signal(self, s: Signal) -> None:
        payload = s.model_dump_json()
        at = s.at.isoformat()
        with self._lock:
            self._conn.execute(
                """INSERT INTO signals_latest (key,payload,updated_at) VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,
                       updated_at=excluded.updated_at""",
                (s.key, payload, at),
            )
            self._conn.execute(
                "INSERT INTO signal_history (key,payload,at) VALUES (?,?,?)",
                (s.key, payload, at),
            )
            self._conn.commit()

    def latest_signals(self) -> list[Signal]:
        rows = self._conn.execute("SELECT payload FROM signals_latest").fetchall()
        return [Signal.model_validate_json(r["payload"]) for r in rows]

    def signal_history(self, key: str, limit: int = 50) -> list[Signal]:
        rows = self._conn.execute(
            "SELECT payload FROM signal_history WHERE key=? ORDER BY id DESC LIMIT ?",
            (key, limit),
        ).fetchall()
        return [Signal.model_validate_json(r["payload"]) for r in rows]

    # ── audit ────────────────────────────────────────────────────────────────
    def add_audit(self, e: AuditEntry) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO audit (at,actor,action,target,detail,ok) VALUES (?,?,?,?,?,?)",
                (e.at.isoformat(), e.actor, e.action, e.target, e.detail, int(e.ok)),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_audit(self, limit: int = 100) -> list[AuditEntry]:
        rows = self._conn.execute(
            "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            AuditEntry(id=r["id"], at=r["at"], actor=r["actor"], action=r["action"],
                       target=r["target"], detail=r["detail"], ok=bool(r["ok"]))
            for r in rows
        ]

    # ── source runs ──────────────────────────────────────────────────────────
    def start_run(self, source: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO source_runs (source,started_at,ok,item_count,signal_count) VALUES (?,?,0,0,0)",
                (source, utcnow().isoformat()),
            )
            self._conn.commit()
            return cur.lastrowid

    def finish_run(self, run_id: int, ok: bool, item_count: int, signal_count: int, detail: str) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE source_runs SET finished_at=?, ok=?, item_count=?,
                       signal_count=?, detail=? WHERE id=?""",
                (utcnow().isoformat(), int(ok), item_count, signal_count, detail, run_id),
            )
            self._conn.commit()

    def list_runs(self, source: Optional[str] = None, limit: int = 50) -> list[SourceRun]:
        if source:
            rows = self._conn.execute(
                "SELECT * FROM source_runs WHERE source=? ORDER BY id DESC LIMIT ?",
                (source, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM source_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [
            SourceRun(id=r["id"], source=r["source"], started_at=r["started_at"],
                      finished_at=r["finished_at"], ok=bool(r["ok"]),
                      item_count=r["item_count"], signal_count=r["signal_count"],
                      detail=r["detail"] or "")
            for r in rows
        ]

    # ── items ────────────────────────────────────────────────────────────────
    def save_items(self, items: list[Item]) -> int:
        new = 0
        with self._lock:
            for it in items:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO items (external_id,source_id,payload,fetched_at) VALUES (?,?,?,?)",
                    (it.external_id, it.source_id, it.model_dump_json(), it.fetched_at.isoformat()),
                )
                new += cur.rowcount
            self._conn.commit()
        return new

    def recent_items(self, source: Optional[str] = None, limit: int = 50) -> list[Item]:
        if source:
            rows = self._conn.execute(
                "SELECT payload FROM items WHERE source_id=? ORDER BY fetched_at DESC LIMIT ?",
                (source, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT payload FROM items ORDER BY fetched_at DESC LIMIT ?", (limit,)).fetchall()
        return [Item.model_validate_json(r["payload"]) for r in rows]

    # ── state history (progress over time; append only when it changes) ──────
    def record_state_snapshot(self, source: str, state: dict) -> bool:
        """Append a source's 'state of play' iff it differs from the last stored one
        (ignoring the volatile 'now' timestamp). Returns True if a new row was written.
        This is the raw material for analysing Luke's progress over time."""
        meaningful = {k: v for k, v in state.items() if k not in ("now",)}
        h = hashlib.sha1(json.dumps(meaningful, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        with self._lock:
            row = self._conn.execute(
                "SELECT hash FROM state_history WHERE source=? ORDER BY id DESC LIMIT 1",
                (source,)).fetchone()
            if row and row["hash"] == h:
                return False
            self._conn.execute(
                "INSERT INTO state_history (source,at,hash,payload) VALUES (?,?,?,?)",
                (source, utcnow().isoformat(), h, json.dumps(state, default=str)))
            self._conn.commit()
            return True

    def state_history(self, source: str, limit: int = 200) -> list[dict]:
        rows = self._conn.execute(
            "SELECT at, payload FROM state_history WHERE source=? ORDER BY id DESC LIMIT ?",
            (source, limit)).fetchall()
        return [{"at": r["at"], "state": json.loads(r["payload"])} for r in rows]

    # ── kv (cursors, misc) ───────────────────────────────────────────────────
    def get_kv(self, k: str) -> Optional[str]:
        row = self._conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row["v"] if row else None

    def set_kv(self, k: str, v: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, v))
            self._conn.commit()
