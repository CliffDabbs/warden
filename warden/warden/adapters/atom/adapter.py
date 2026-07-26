"""Atom Learning adapter — API-first, Google-SSO session.

Atom is a SPA over a clean JSON API at ``api.atomlearning.com`` (auth = an HttpOnly
cookie on ``.atomlearning.com``). We hit that API directly with httpx — no DOM
scraping — per docs/atom-adapter-HANDOVER.md.

Auth is Google sign-in, which can't be reliably automated headlessly. So we **capture
the session once**: run ``python -m warden.adapters.atom.login`` to sign in with Google
by hand in a real browser; it saves a Playwright storage_state JSON (the cookie jar).
This adapter loads those ``.atomlearning.com`` cookies into httpx and calls the API
headlessly — which also works in the container (no browser needed at collect time).
When the session expires, re-run the login helper.

Endpoints used (GET, cookie-auth):
  /ms_accounts/user                                account + student profiles
  /ms_learning/students/{sid}/islands              per-topic completion history
  /ms_learning/students/{sid}/learning-resources   set work / to-do list
  /ms_learning/students/{sid}/subjects             subject config (optional)

Signals for `subject` (ctx.subject or "luke"):
  atom.<subject>.daily_complete           (bool)   did the set work + activity today
  atom.<subject>.islands_completed_today  (number) topics completed today
  atom.<subject>.assignments_outstanding  (number) parent-set work not yet started

Items:
  kind "activity"   — a completed island (external_id atom:island:<id_island_progression>)
  kind "assignment" — set work        (external_id atom:resource:<id_question_session>)

If there's no saved session (or a live call fails) it falls back to fixtures/, so the
app stays demoable offline.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ...models import Item, Signal, SignalType, utcnow
from ..base import (
    AdapterManifest, CollectResult, HealthResult, SourceAdapter, SourceContext,
)

_HERE = Path(__file__).resolve().parent            # …/warden/warden/adapters/atom
_MANIFEST_PATH = _HERE / "manifest.json"
_REPO_ROOT = _HERE.parents[2]                       # …/warden  (matches login.py's default)
_UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 Edg/126.0"),
    "Accept": "application/json, text/plain, */*",
    # Atom's API sits behind app.atomlearning.com; send the same Origin/Referer a browser does.
    "Origin": "https://app.atomlearning.com",
    "Referer": "https://app.atomlearning.com/",
}


# ── small helpers ────────────────────────────────────────────────────────────
def _as_list(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """Unwrap an API payload that may be a bare list or {data|<key>: [...]}"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("data", "results", *keys):
            v = payload.get(k)
            if isinstance(v, list):
                return v
    return []


def _parse_dt(s: Any) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _tz(ctx: SourceContext) -> ZoneInfo:
    try:
        return ZoneInfo(ctx.options.get("tz", "Europe/London"))
    except Exception:
        return ZoneInfo("UTC")


def _is_parent_set(res: dict[str, Any]) -> bool:
    roles = (res.get("setBy") or {}).get("roles") or {}
    return bool(roles.get("prnt") or roles.get("sup"))


class Adapter(SourceAdapter):
    manifest: AdapterManifest = AdapterManifest.model_validate_json(
        _MANIFEST_PATH.read_text(encoding="utf-8")
    )

    # ── contract ops ─────────────────────────────────────────────────────────
    async def authenticate(self, ctx: SourceContext) -> dict[str, Any]:
        if not ctx.live:
            return {"mode": "fixtures"}
        state = self._state_path(ctx)
        if self._load_cookies(state):
            return {"mode": "live", "state_path": str(state)}
        return {
            "mode": "fixtures",
            "warning": (f"no saved Atom session at {state} — run "
                        "`python -m warden.adapters.atom.login` to sign in with Google"),
        }

    async def collect(self, ctx: SourceContext, session: dict[str, Any]) -> CollectResult:
        subject = ctx.subject or "luke"
        if session.get("mode") == "live":
            try:
                return await self._collect_live(ctx, subject)
            except Exception as e:
                return self._collect_fixtures(
                    ctx, subject, warning=f"live failed ({type(e).__name__}: {e}); used fixtures")
        return self._collect_fixtures(ctx, subject, warning=session.get("warning", ""))

    async def healthcheck(self, ctx: SourceContext) -> HealthResult:
        state = self._state_path(ctx)
        if ctx.live and self._load_cookies(state):
            try:
                import httpx
                api = ctx.options.get("api_base", "https://api.atomlearning.com").rstrip("/")
                async with self._client(ctx, api) as client:
                    r = await client.get("/ms_accounts/user")
                if r.status_code in (401, 403):
                    return HealthResult(ok=False, detail="session expired — re-run atom login")
                return HealthResult(ok=r.status_code < 400, detail=f"user {r.status_code}")
            except Exception as e:
                return HealthResult(ok=False, detail=f"unreachable: {type(e).__name__}")
        ok = (_HERE / "fixtures" / "islands.json").exists()
        return HealthResult(ok=ok, detail="fixtures present" if ok else "fixtures missing")

    # ── live (JSON API) ──────────────────────────────────────────────────────
    def _state_path(self, ctx: SourceContext) -> Path:
        raw = ctx.options.get("state_file") or "data/atom_state.json"
        p = Path(raw)
        return p if p.is_absolute() else (_REPO_ROOT / p)

    @staticmethod
    def _load_cookies(state_path: Path) -> list[dict[str, Any]]:
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        cookies = data.get("cookies") if isinstance(data, dict) else None
        if not isinstance(cookies, list):
            return []
        return [c for c in cookies if "atomlearning.com" in (c.get("domain") or "")]

    def _client(self, ctx: SourceContext, api_base: str):
        import httpx
        jar = httpx.Cookies()
        for c in self._load_cookies(self._state_path(ctx)):
            jar.set(c["name"], c.get("value", ""),
                    domain=(c.get("domain") or "").lstrip("."), path=c.get("path", "/"))
        return httpx.AsyncClient(base_url=api_base, cookies=jar, headers=_UA,
                                 timeout=20.0, follow_redirects=True)

    async def _resolve_student(self, client, ctx: SourceContext) -> str:
        configured = ctx.options.get("student_id")
        if configured:
            return str(configured)
        r = await client.get("/ms_accounts/user")
        r.raise_for_status()
        user = r.json()
        # student lists live under a few possible keys depending on the account shape
        students = []
        if isinstance(user, dict):
            for k in ("students", "children", "profiles", "learners"):
                v = user.get(k)
                if isinstance(v, list) and v:
                    students = v
                    break
        if not students:
            raise RuntimeError("could not resolve a student id; set options.student_id")
        want = (ctx.subject or "").lower()
        for s in students:
            name = " ".join(str(s.get(k, "")) for k in ("firstName", "name", "displayName")).lower()
            if want and want in name:
                return str(s.get("id") or s.get("studentId") or s.get("id_student"))
        s = students[0]
        return str(s.get("id") or s.get("studentId") or s.get("id_student"))

    async def _collect_live(self, ctx: SourceContext, subject: str) -> CollectResult:
        api = ctx.options.get("api_base", "https://api.atomlearning.com").rstrip("/")
        async with self._client(ctx, api) as client:
            sid = await self._resolve_student(client, ctx)
            ri = await client.get(f"/ms_learning/students/{sid}/islands")
            if ri.status_code in (401, 403):
                raise RuntimeError("session expired (401/403) — re-run atom login")
            ri.raise_for_status()
            rr = await client.get(f"/ms_learning/students/{sid}/learning-resources")
            islands = _as_list(ri.json(), "islands")
            resources = _as_list(rr.json(), "learningResources", "resources") if rr.status_code < 400 else []
            # attainment scores (0..1 per subject + overall, with monthly history) and
            # mock-test results (questionsCorrect/total) — best-effort, never fatal
            scores = await self._get_list(client, f"/ms_learning/students/{sid}/score?hierarchyLevel=SUMMARY,SUBJECT&history=true")
            mocks = await self._get_list(client, f"/ms_mocks/students/{sid}/mock_tests?type=HOME_MOCK")
            # Atom's atom-auth cookie is a SLIDING session — the API re-issues it on
            # every call. Save the refreshed jar so the session stays alive indefinitely
            # as long as we keep polling (capture-once, runs-forever — even in a container).
            self._persist_refreshed_cookies(client, self._state_path(ctx))
        return self._build(ctx, subject, islands, resources, live=True, scores=scores, mocks=mocks)

    @staticmethod
    async def _get_list(client, path: str) -> list[dict[str, Any]]:
        try:
            r = await client.get(path)
            return _as_list(r.json()) if r.status_code < 400 else []
        except Exception:
            return []

    @staticmethod
    def _persist_refreshed_cookies(client, state_path: Path) -> None:
        """Write the refreshed atomlearning cookies (esp. the sliding atom-auth) back
        into the storage-state file, so the next run uses the extended session."""
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        cookies = state.get("cookies")
        if not isinstance(cookies, list):
            return
        by_name: dict[str, dict[str, Any]] = {c.get("name"): c for c in cookies}
        changed = False
        for ck in client.cookies.jar:                       # http.cookiejar.Cookie objects
            if "atomlearning.com" not in (ck.domain or ""):
                continue
            cur = by_name.get(ck.name)
            if cur is None:
                continue                                    # only refresh cookies we already have
            if cur.get("value") != ck.value or (ck.expires and cur.get("expires") != ck.expires):
                cur["value"] = ck.value
                if ck.expires:
                    cur["expires"] = ck.expires
                changed = True
        if changed:
            try:
                state_path.write_text(json.dumps(state), encoding="utf-8")
            except Exception:
                pass

    # ── fixtures (offline) ───────────────────────────────────────────────────
    def _collect_fixtures(self, ctx: SourceContext, subject: str, warning: str = "") -> CollectResult:
        fx = _HERE / "fixtures"
        islands = _as_list(self._read(fx / "islands.json"), "islands")
        resources = _as_list(self._read(fx / "learning-resources.json"), "learningResources")
        # make the demo "live": stamp any island flagged today with a real now-ish time,
        # incl. its session's started/completed so the state-of-play shows minutes today
        now_dt = utcnow()
        now = now_dt.isoformat()
        for isl in islands:
            if isl.get("today"):
                isl["dateCompleted"] = now
                isl["status"] = "completed"
                for s in (isl.get("sessions") or [{}]):
                    s["completed"] = now
                    s["started"] = (now_dt - timedelta(minutes=16)).isoformat()
                if not isl.get("sessions"):
                    isl["sessions"] = [{"completed": now,
                                        "started": (now_dt - timedelta(minutes=16)).isoformat()}]
        return self._build(ctx, subject, islands, resources, live=False, warning=warning)

    @staticmethod
    def _read(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []

    # ── shared normalisation → Items + Signals ───────────────────────────────
    def _build(self, ctx: SourceContext, subject: str, islands: list[dict[str, Any]],
               resources: list[dict[str, Any]], live: bool, warning: str = "",
               scores: Optional[list] = None, mocks: Optional[list] = None) -> CollectResult:
        tz = _tz(ctx)
        today = datetime.now(tz).date()
        since = _parse_dt(ctx.since_cursor)

        completed = [i for i in islands if i.get("status") == "completed" and i.get("dateCompleted")]
        completed_today = [
            i for i in completed
            if (dt := _parse_dt(i["dateCompleted"])) and dt.astimezone(tz).date() == today
        ]
        assignments = [r for r in resources if _is_parent_set(r)]
        outstanding = [r for r in assignments if not r.get("started")]

        # thresholds (configurable)
        target = int(ctx.options.get("daily_target_islands", 1))
        require_done = bool(ctx.options.get("require_assignments_done", True))
        daily_complete = (len(completed_today) >= target
                          and (len(outstanding) == 0 or not require_done))

        # ── items ─────────────────────────────────────────────────────────────
        items: list[Item] = []
        # activity items: islands completed since the cursor (else just today's), newest first
        recent = [i for i in completed if (dt := _parse_dt(i["dateCompleted"]))
                  and (since is None or dt > since)]
        recent.sort(key=lambda i: _parse_dt(i["dateCompleted"]) or utcnow(), reverse=True)
        for isl in recent[:50]:
            dt = _parse_dt(isl.get("dateCompleted"))
            items.append(Item(
                source_id="atom", subject_ids=[subject],
                external_id=f"atom:island:{isl.get('id_island_progression') or isl.get('title')}",
                kind="activity", title=str(isl.get("title") or "Atom activity"),
                body_text="Completed" + (f" · {dt.astimezone(tz):%d %b %H:%M}" if dt else ""),
                occurred_at=dt, audience_tags=["practice"], raw=isl,
            ))
        # assignment items: parent-set work (surface started/not-started + due)
        for res in assignments:
            items.append(Item(
                source_id="atom", subject_ids=[subject],
                external_id=f"atom:resource:{res.get('id_question_session') or res.get('id')}",
                kind="assignment", title=str(res.get("name") or "Set work"),
                body_text=("Not started" if not res.get("started") else "Started")
                          + (f" · set by {(res.get('setBy') or {}).get('name','?')}"),
                occurred_at=_parse_dt(res.get("createdAt")),
                due_at=_parse_dt(res.get("dueDate")),
                audience_tags=[str(res.get("type") or "practice")], raw=res,
            ))

        # ── signals ───────────────────────────────────────────────────────────
        last = max((_parse_dt(i["dateCompleted"]) for i in completed
                    if _parse_dt(i["dateCompleted"])), default=None)
        signals = [
            Signal(key=f"atom.{subject}.daily_complete", value=daily_complete,
                   type=SignalType.bool, source="atom", subject=subject,
                   meta={"completed_today": len(completed_today),
                         "assignments_outstanding": len(outstanding)}),
            Signal(key=f"atom.{subject}.islands_completed_today", value=len(completed_today),
                   type=SignalType.number, source="atom", subject=subject),
            Signal(key=f"atom.{subject}.assignments_outstanding", value=len(outstanding),
                   type=SignalType.number, source="atom", subject=subject),
        ]

        cursor = last.isoformat() if last else ctx.since_cursor
        mode = "live" if live else "fixtures"
        detail = (f"{mode}: {len(completed_today)} done today, "
                  f"{len(outstanding)} set-work outstanding, {len(completed)} completed all-time")
        if warning:
            detail += f" — {warning}"
        return CollectResult(ok=True, items=items, signals=signals, next_cursor=cursor,
                             detail=detail,
                             state=self._state_of_play(ctx, islands, resources, scores, mocks))

    # ── "state of play" for the dynamic LLM evaluator ────────────────────────
    _SESSION_CAP_MIN = 45.0        # a practice session left open shouldn't count as hours

    def _state_of_play(self, ctx: SourceContext, islands: list[dict[str, Any]],
                       resources: list[dict[str, Any]], scores: Optional[list] = None,
                       mocks: Optional[list] = None) -> dict[str, Any]:
        tz = _tz(ctx)
        now = datetime.now(tz)
        today = now.date()

        # subject id -> name (from the score endpoint) so topics read nicely
        subj_names: dict[Any, str] = {}
        for row in (scores or []):
            if row.get("id_subject") is not None and row.get("subjectTitle"):
                subj_names[row["id_subject"]] = row["subjectTitle"]

        per_day: dict[Any, dict[str, Any]] = {}
        for isl in islands:
            if isl.get("status") != "completed":
                continue
            for s in (isl.get("sessions") or []):
                st, cp = _parse_dt(s.get("started")), _parse_dt(s.get("completed"))
                if not (st and cp):
                    continue
                mins = (cp - st).total_seconds() / 60.0
                if mins <= 0:
                    continue
                mins = round(min(mins, self._SESSION_CAP_MIN), 1)
                day = cp.astimezone(tz).date()
                e = per_day.setdefault(day, {"minutes": 0.0, "topics": []})
                e["minutes"] = round(e["minutes"] + mins, 1)
                e["topics"].append({"title": isl.get("title"),
                                    "subject": subj_names.get(isl.get("id_course_subject"),
                                                              isl.get("id_course_subject")),
                                    "minutes": mins, "at": cp.astimezone(tz).strftime("%H:%M")})
        days = sorted(per_day, reverse=True)
        last = days[0] if days else None
        assignments = [
            {"name": r.get("name"), "type": r.get("type"),
             "started": bool(r.get("started")), "due": r.get("dueDate"),
             "set_by": (r.get("setBy") or {}).get("name")}
            for r in resources if (r.get("setBy") or {}).get("roles", {}).get("prnt")
        ]

        state: dict[str, Any] = {
            "source": "atom", "subject": ctx.subject or "luke",
            "now": now.strftime("%Y-%m-%d %H:%M (%A)"),
            "today": {"date": str(today), **per_day.get(today, {"minutes": 0.0, "topics": []})},
            "last_active_day": ({"date": str(last), **per_day[last]} if last else None),
            "recent_days_minutes": {str(d): per_day[d]["minutes"] for d in days[:7]},
            "assignments_parent_set": assignments,
            "totals": {"islands_completed_all_time":
                       sum(1 for i in islands if i.get("status") == "completed")},
        }

        # attainment scores (0..1; 1.0 ≈ on target, can exceed) + mastery 1-3, per subject
        if scores:
            overall = next((r.get("attainmentScore") for r in scores
                            if r.get("hierarchyLevel") == "SUMMARY"), None)
            subs = {r["subjectTitle"]: round(r.get("attainmentScore") or 0, 3)
                    for r in scores if r.get("hierarchyLevel") == "SUBJECT" and r.get("subjectTitle")}
            mastery = {r["subjectTitle"]: r.get("masteryLevel")
                       for r in scores if r.get("hierarchyLevel") == "SUBJECT" and r.get("subjectTitle")}
            state["attainment"] = {
                "overall_0to1": round(overall, 3) if overall is not None else None,
                "by_subject_0to1": subs, "mastery_1to3": mastery,
                "scale": "attainmentScore ~0..1 (1.0 = on target, can exceed); masteryLevel 1-3",
            }
        # recent mock-test results (exact % correct)
        if mocks:
            done = [m for m in mocks if m.get("finished")
                    and m.get("questionsCorrect") is not None and m.get("totalQuestions")]
            done.sort(key=lambda m: m.get("finished") or "", reverse=True)
            state["recent_mock_tests"] = [
                {"name": m.get("name"), "finished": str(m.get("finished"))[:10],
                 "correct": m.get("questionsCorrect"), "total": m.get("totalQuestions"),
                 "percent": round(100 * m["questionsCorrect"] / m["totalQuestions"]),
                 "percentile": m.get("percentile")}
                for m in done[:5]
            ]
        return state
