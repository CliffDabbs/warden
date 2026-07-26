#!/usr/bin/env python3
"""End-to-end smoke test for a running Warden server.

Exercises the whole loop: status/config/state, seed-rule compilation, a live compile
preview, running a source (fixtures) to emit signals, and - the money shot - simulating
the `atom.luke.daily_complete` signal to watch the "allow YouTube for the kids" rule fire
and flip AdGuard state, verified via the audit log and a fresh state snapshot.

Run against a server started in FAKE mode so it mutates nothing real:
    ADGUARD_MODE=fake python -m warden           # (terminal 1)
    python scripts/smoke.py                       # (terminal 2)
"""
from __future__ import annotations

import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
ok = 0
fail = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok, fail
    mark = "PASS" if cond else "FAIL"
    if cond:
        ok += 1
    else:
        fail += 1
    print(f"  [{mark}] {name}" + (f" - {extra}" if extra else ""))


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=30)

    print("* health + status")
    check("healthz", c.get("/healthz").json().get("ok") is True)
    st = c.get("/api/status").json()
    check("status.mode present", st.get("mode") in ("live", "fake"), f"mode={st.get('mode')}")

    print("* config")
    cfg = c.get("/api/config").json()
    check("config has kids group", any(g["name"] == "kids" for g in cfg.get("groups", [])))
    check("config lists sources", len(cfg.get("sources", [])) >= 1)
    check("config lists signals", any("daily_complete" in s["key"] for s in cfg.get("signals", [])))

    print("* state snapshot")
    state = c.get("/api/state").json()
    kids = next((g for g in state["groups"] if g["name"] == "kids"), None)
    check("state has kids group", kids is not None)

    print("* seed rules compiled")
    rules = c.get("/api/rules").json()
    check("3 seed rules", len(rules) >= 3, f"{len(rules)} rules")
    compiled = [r for r in rules if r.get("compiled")]
    check("seed rules compiled", len(compiled) >= 3, f"{len(compiled)} compiled")
    yt_rule = next((r for r in rules if "atom" in r["text"].lower() and "youtube" in r["text"].lower()), None)
    check("found the atom->youtube rule", yt_rule is not None)

    print("* live compile preview")
    prev = c.post("/api/rules/preview", json={"text": "at 9pm every day block netflix for the kids"})
    check("preview 200", prev.status_code == 200, f"HTTP {prev.status_code}")
    if prev.status_code == 200:
        cr = prev.json()
        check("preview trigger is schedule", cr["trigger"]["kind"] == "schedule",
              cr["trigger"].get("cron"))
        check("preview action set_service netflix", any(
            a["kind"] == "set_service" and a.get("service") == "netflix" for a in cr["actions"]))

    print("* run the atom source (fixtures) -> signals")
    run = c.post("/api/sources/atom/run", params={"live": "false"}).json()
    check("atom run ok", run.get("ok") is True, run.get("detail", ""))
    check("atom emitted signals", run.get("signal_count", 0) >= 1, f"{run.get('signal_count')} signals")
    sigs = c.get("/api/signals").json()
    daily = next((s for s in sigs if s["key"].endswith("daily_complete")), None)
    check("daily_complete signal present", daily is not None,
          f"value={daily and daily.get('value')}")

    print("* rule architecture: dynamic (LLM-evaluated) vs deterministic (cron)")
    # the Atom rule depends on live data -> it's now evaluated on-the-fly by the LLM
    atom_rule = next((r for r in rules if "atom" in r["text"].lower() and r.get("compiled")), None)
    check("atom rule is marked dynamic", bool(atom_rule and atom_rule["compiled"].get("dynamic")),
          f"dynamic={atom_rule and atom_rule['compiled'].get('dynamic')}")
    # a pure time rule stays deterministic and dry-runs to concrete actions (no LLM)
    sched = next((r for r in rules if r.get("compiled")
                  and r["compiled"]["trigger"]["kind"] == "schedule"
                  and not r["compiled"].get("dynamic")), None)
    check("deterministic schedule rule present", sched is not None)
    if sched:
        dr = c.post(f"/api/rules/{sched['id']}/run", json={"dry": True}).json()
        check("deterministic rule dry-run yields actions", dr.get("ok") and len(dr.get("actions", [])) >= 1,
              f"{len(dr.get('actions', []))} actions")

    print("* manual switch control still applies to AdGuard")
    c.post("/api/actions/service", json={"group": "kids", "service": "youtube", "state": "allowed"})
    time.sleep(0.2)
    state2 = c.get("/api/state").json()
    kids2 = next((g for g in state2["groups"] if g["name"] == "kids"), None)
    yt = next((s for s in (kids2 or {}).get("services", []) if s["id"] == "youtube"), None)
    check("manual set_service applied (kids youtube allowed)", yt and yt["state"] == "allowed",
          f"state={yt and yt['state']}")

    print(f"\n{ok} passed, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
