"""SQLite state store — rules, signals, audit, LLM calls, source runs, items, cursors.

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

from .models import (
    AuditEntry, DeviceOverride, LlmCall, Signal, SignalType, SourceRun, Item, utcnow,
)

# How many LLM exchanges to keep, and how much of each. A world-state prompt runs to
# tens of KB, so the log is trimmed on write rather than allowed to grow without bound —
# recent calls are what anyone actually asks about, and the audit line survives the
# trim even when the transcript behind it doesn't.
LLM_LOG_KEEP = 300
LLM_FIELD_CHARS = 200_000
from .rules.schema import Rule

# Fields an adapter re-stamps on every collection. They say when we looked, never what
# we found, so they are stripped before a state snapshot is compared with the last one.
_CLOCK_FIELDS = ("now", "collected_at", "fetched_at", "generated_at")


def _without_clocks(value):
    """Deep-copy `value` minus the run-stamp fields, for change detection only."""
    if isinstance(value, dict):
        return {k: _without_clocks(v) for k, v in value.items() if k not in _CLOCK_FIELDS}
    if isinstance(value, list):
        return [_without_clocks(v) for v in value]
    return value


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
    last_result TEXT,
    next_check_at TEXT,
    next_check_reason TEXT
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
    at TEXT, actor TEXT, action TEXT, target TEXT, detail TEXT, ok INTEGER,
    ref TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    purpose TEXT, backend TEXT, model TEXT,
    system TEXT, prompt TEXT, response TEXT,     -- exactly what was sent and received
    ok INTEGER NOT NULL DEFAULT 1,
    ms INTEGER, input_tokens INTEGER, output_tokens INTEGER,
    error TEXT
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
CREATE TABLE IF NOT EXISTS switch_pins (
    -- A parent's manual flip of one switch. While a pin is live the DYNAMIC rule
    -- evaluator may not change that switch; a SCHEDULED rule writing it applies and
    -- clears the pin (the nightly switchoff is the reset). expires_at is only the
    -- safety net for switches no schedule ever touches.
    group_name TEXT NOT NULL,
    service TEXT NOT NULL,
    state TEXT NOT NULL,          -- the state the parent chose
    actor TEXT DEFAULT 'user',
    created_at TEXT,
    expires_at TEXT,
    PRIMARY KEY (group_name, service)
);
CREATE TABLE IF NOT EXISTS overrides (
    client TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'unblock',
    created_at TEXT,
    expires_at TEXT,            -- NULL = no expiry ("forever", until cancelled)
    actor TEXT DEFAULT 'user'
);
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
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns that CREATE TABLE IF NOT EXISTS won't add to an existing db.

        Cheap and idempotent: read the current columns, ALTER in whatever's missing.
        Without this, an upgrade over a pre-existing warden.db fails at query time
        rather than at startup, which is a much worse place to find out.
        """
        wanted = {
            "rules": [("next_check_at", "TEXT"), ("next_check_reason", "TEXT")],
            "audit": [("ref", "TEXT")],
        }
        for table, cols in wanted.items():
            try:
                have = {r["name"] for r in
                        self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            except sqlite3.Error:
                continue
            for name, decl in cols:
                if name not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # ── rules ────────────────────────────────────────────────────────────────
    def upsert_rule(self, rule: Rule) -> None:
        now = utcnow().isoformat()
        rule.created_at = rule.created_at or now
        rule.updated_at = now
        compiled = rule.compiled.model_dump_json() if rule.compiled else None
        with self._lock:
            self._conn.execute(
                """INSERT INTO rules (id,text,enabled,compiled,compile_error,source,
                       created_at,updated_at,last_fired_at,last_result,
                       next_check_at,next_check_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       text=excluded.text, enabled=excluded.enabled,
                       compiled=excluded.compiled, compile_error=excluded.compile_error,
                       source=excluded.source, updated_at=excluded.updated_at,
                       last_fired_at=excluded.last_fired_at, last_result=excluded.last_result,
                       next_check_at=excluded.next_check_at,
                       next_check_reason=excluded.next_check_reason""",
                (rule.id, rule.text, int(rule.enabled), compiled, rule.compile_error,
                 rule.source, rule.created_at, rule.updated_at,
                 rule.last_fired_at, rule.last_result,
                 rule.next_check_at, rule.next_check_reason),
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
            next_check_at=self._col(row, "next_check_at"),
            next_check_reason=self._col(row, "next_check_reason"),
        )

    @staticmethod
    def _col(row: sqlite3.Row, name: str) -> Optional[str]:
        """Tolerate a row from a db that predates a column (belt-and-braces to _migrate)."""
        try:
            return row[name]
        except (IndexError, KeyError):
            return None

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

    def touch_rule(self, rid: str, last_fired_at: str, last_result: str,
                   next_check_at: Optional[str] = None,
                   next_check_reason: Optional[str] = None) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE rules SET last_fired_at=?, last_result=?,
                       next_check_at=?, next_check_reason=? WHERE id=?""",
                (last_fired_at, last_result, next_check_at, next_check_reason, rid),
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
                "INSERT INTO audit (at,actor,action,target,detail,ok,ref) VALUES (?,?,?,?,?,?,?)",
                (e.at.isoformat(), e.actor, e.action, e.target, e.detail, int(e.ok), e.ref),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_audit(self, limit: int = 100) -> list[AuditEntry]:
        rows = self._conn.execute(
            "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            AuditEntry(id=r["id"], at=r["at"], actor=r["actor"], action=r["action"],
                       target=r["target"], detail=r["detail"], ok=bool(r["ok"]),
                       ref=(r["ref"] if "ref" in r.keys() else "") or "")
            for r in rows
        ]

    # ── LLM calls (the exact prompt + reply behind an "llm_call" audit line) ──
    def add_llm_call(self, c: LlmCall) -> int:
        """Store one exchange and trim the log to the last LLM_LOG_KEEP.

        Truncation is marked in the text itself rather than done silently: a reader has
        to be able to tell "this is all of it" from "this is the start of it".
        """
        def clip(s: str) -> str:
            s = s or ""
            return s if len(s) <= LLM_FIELD_CHARS else (
                s[:LLM_FIELD_CHARS] + f"\n\n… [truncated: {len(s)} characters in total]")

        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO llm_calls (at,purpose,backend,model,system,prompt,response,"
                "ok,ms,input_tokens,output_tokens,error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (c.at.isoformat(), c.purpose, c.backend, c.model, clip(c.system),
                 clip(c.prompt), clip(c.response), int(c.ok), c.ms,
                 c.input_tokens, c.output_tokens, c.error),
            )
            self._conn.execute(
                "DELETE FROM llm_calls WHERE id <= (SELECT MAX(id) FROM llm_calls) - ?",
                (LLM_LOG_KEEP,))
            self._conn.commit()
            return cur.lastrowid

    def get_llm_call(self, call_id: int) -> Optional[LlmCall]:
        r = self._conn.execute("SELECT * FROM llm_calls WHERE id=?", (call_id,)).fetchone()
        return self._llm_row(r) if r else None

    def list_llm_calls(self, limit: int = 50) -> list[LlmCall]:
        """Newest first, WITHOUT the bodies — a listing, not a transcript dump."""
        rows = self._conn.execute(
            "SELECT id,at,purpose,backend,model,ok,ms,input_tokens,output_tokens,error,"
            "length(system) AS sys_len, length(prompt) AS prompt_len,"
            "length(response) AS resp_len FROM llm_calls ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        return [LlmCall(id=r["id"], at=r["at"], purpose=r["purpose"] or "",
                        backend=r["backend"] or "", model=r["model"] or "",
                        ok=bool(r["ok"]), ms=r["ms"] or 0,
                        input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
                        error=r["error"] or "",
                        system=f"[{r['sys_len'] or 0} characters]",
                        prompt=f"[{r['prompt_len'] or 0} characters]",
                        response=f"[{r['resp_len'] or 0} characters]")
                for r in rows]

    @staticmethod
    def _llm_row(r) -> LlmCall:
        return LlmCall(id=r["id"], at=r["at"], purpose=r["purpose"] or "",
                       backend=r["backend"] or "", model=r["model"] or "",
                       system=r["system"] or "", prompt=r["prompt"] or "",
                       response=r["response"] or "", ok=bool(r["ok"]), ms=r["ms"] or 0,
                       input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
                       error=r["error"] or "")

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
        """Append a source's 'state of play' iff it differs from the last stored one.

        "Differs" means the FACTS moved, so every clock field is stripped before
        hashing, at any depth — an adapter stamps `now` at the top and `collected_at`
        inside data_quality, and with those in the hash every poll looked like a change
        (an hour of identical Atom data archived 15 times a day). The return value is
        load-bearing beyond the archive: SourceRegistry uses it to decide whether new
        data is worth waking a sleeping rule for, and "always changed" is the same as
        "never changed" for that purpose.
        """
        h = hashlib.sha1(json.dumps(_without_clocks(state), sort_keys=True,
                                    default=str).encode("utf-8")).hexdigest()
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
    # ── device overrides ─────────────────────────────────────────────────────
    # An override exempts ONE device from Warden's group writes until it expires,
    # so a scheduled rule can't silently undo a parent's manual unblock.
    def upsert_override(self, o: DeviceOverride) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO overrides (client, kind, created_at, expires_at, actor) "
                "VALUES (?,?,?,?,?) ON CONFLICT(client) DO UPDATE SET "
                "kind=excluded.kind, created_at=excluded.created_at, "
                "expires_at=excluded.expires_at, actor=excluded.actor",
                (o.client, o.kind, o.created_at, o.expires_at, o.actor),
            )
            self._conn.commit()

    def list_overrides(self) -> list[DeviceOverride]:
        rows = self._conn.execute("SELECT * FROM overrides ORDER BY client").fetchall()
        return [DeviceOverride(**dict(r)) for r in rows]

    def get_override(self, client: str) -> Optional[DeviceOverride]:
        r = self._conn.execute("SELECT * FROM overrides WHERE client=?", (client,)).fetchone()
        return DeviceOverride(**dict(r)) if r else None

    def delete_override(self, client: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM overrides WHERE client=?", (client,))
            self._conn.commit()

    def expired_overrides(self, now_iso: str) -> list[DeviceOverride]:
        """Overrides whose time is up. NULL expires_at ('forever') never qualifies."""
        rows = self._conn.execute(
            "SELECT * FROM overrides WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now_iso,),
        ).fetchall()
        return [DeviceOverride(**dict(r)) for r in rows]

    # ── switch pins (manual flips that outrank the dynamic evaluator) ────────
    def pin_switch(self, group: str, service: str, state: str, actor: str,
                   expires_at: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO switch_pins (group_name,service,state,actor,created_at,expires_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(group_name,service) DO UPDATE SET "
                "state=excluded.state, actor=excluded.actor, "
                "created_at=excluded.created_at, expires_at=excluded.expires_at",
                (group, service, state, actor, utcnow().isoformat(), expires_at))
            self._conn.commit()

    def active_pins(self, now_iso: str) -> dict[tuple[str, str], dict]:
        """Live pins as {(group, service): row}. Expired rows are pruned as a side
        effect so the table cannot accumulate stale holds."""
        with self._lock:
            self._conn.execute("DELETE FROM switch_pins WHERE expires_at <= ?", (now_iso,))
            self._conn.commit()
            rows = self._conn.execute("SELECT * FROM switch_pins").fetchall()
        return {(r["group_name"], r["service"]): dict(r) for r in rows}

    def clear_pins(self, switches: list[tuple[str, str]]) -> int:
        """Remove pins for these (group, service) pairs; returns how many existed."""
        if not switches:
            return 0
        n = 0
        with self._lock:
            for g, svc in switches:
                cur = self._conn.execute(
                    "DELETE FROM switch_pins WHERE group_name=? AND service=?", (g, svc))
                n += cur.rowcount
            self._conn.commit()
        return n

    def active_override_names(self, now_iso: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT client FROM overrides WHERE expires_at IS NULL OR expires_at > ?",
            (now_iso,),
        ).fetchall()
        return {r["client"] for r in rows}

    def get_kv(self, k: str) -> Optional[str]:
        row = self._conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row["v"] if row else None

    def set_kv(self, k: str, v: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, v))
            self._conn.commit()

    def del_kv(self, k: str) -> bool:
        """Forget a key. Returns whether there was one — used to drop a weekly target
        override so the source's own published number takes over again."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM kv WHERE k=?", (k,))
            self._conn.commit()
            return cur.rowcount > 0
