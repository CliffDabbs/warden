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
  /ms_accounts/students/{sid}                      profile (year group)
  /ms_learning/students/{sid}/islands              per-topic completion history
  /ms_learning/students/{sid}/learning-resources   set work / to-do list
  /ms_learning/students/{sid}/learning-journey-targets   THIS WEEK'S PLAN (per subject)
  /ms_subscriptions/students/{sid}/enrolments      the course the plan belongs to

Signals for `subject` (ctx.subject or "luke"):
  atom.<subject>.daily_complete           (bool)   did the set work + activity today
  atom.<subject>.islands_completed_today  (number) islands completed today
  atom.<subject>.islands_due_today        (number) today's share of the weekly target
  atom.<subject>.assignments_outstanding  (number) parent-set work not yet started

The weekly target is ATOM'S OWN, read from `learning-journey-targets`: per subject, for
this child's year group and course, for this ISO week. It is the number Luke's dashboard
divides by ("English 0/10 islands done"), and it moves every week — 17 in the week of
31 Aug, 20 islands + 1 mock test the week after. A mock test counts as one item of the
plan, exactly as the dashboard counts it, so the target is islands + mock tests and a
finished mock counts toward the week. A parent override for this week still wins, and the
configured `weekly_island_target` is only the fallback for a plan we cannot read. `_pace`
derives today's share from whichever number won — see `_target_block`.

Items:
  kind "activity"   — a completed island (external_id atom:island:<id_island_progression>)
  kind "assignment" — set work        (external_id atom:resource:<id_question_session>)

If there's no saved session (or a live call fails) it falls back to fixtures/, so the
app stays demoable offline.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ...models import Item, Signal, SignalType, utcnow
from ..base import (
    AdapterManifest, CollectResult, HealthResult, SourceAdapter, SourceContext,
)

log = logging.getLogger("warden.adapters.atom")

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


def _iso_week(d) -> tuple[int, str]:
    """(week number, "YYYY-Www") for a date — Atom keys its weekly plan by ISO week."""
    year, week, _ = d.isocalendar()
    return week, f"{year}-W{week:02d}"


def _as_int(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        log.warning("atom: unusable weekly island target %r — ignoring", raw)
        return None


def _resolve_target(ctx: SourceContext,
                    plan: Optional[dict[str, Any]]) -> tuple[Optional[int], str]:
    """This week's target, and where it came from.

    Atom publishes its own plan for the week and that is what the child is shown, so it
    is the default — not a figure typed into Warden weeks ago that has been silently
    wrong ever since. Precedence:

      1. a parent override for THIS week (illness, a holiday, a week worth pushing) —
         deliberate, and it must not be overwritten by the plan;
      2. Atom's published plan for this ISO week;
      3. the configured `weekly_island_target`, for a plan we could not read (offline
         fixtures, a failed call) — the hub also parks the last plan it saw there, so a
         one-off API hiccup keeps last night's number rather than falling back years.

    Anything unreadable counts as "not set" rather than being guessed at.
    """
    option = _as_int(ctx.options.get("weekly_island_target"))
    origin = ctx.options.get("weekly_island_target_origin", "config-default")
    if origin == "override" and option is not None:
        return option, "override"
    if plan and plan.get("total") is not None:
        return int(plan["total"]), "atom-published"
    if option is None:
        return None, "unset"
    return option, origin


def _mocks_finished(mocks: Optional[list], tz, start, end) -> list[dict[str, Any]]:
    """Mock tests Atom recorded as finished between two dates (inclusive).

    Atom's weekly plan counts a mock test as one of its items — a 9-island English target
    with one mock set reads "0/10 islands done" on the dashboard — so Warden counts one
    the same way. Without this a week containing a mock could never be finished, and its
    last day would demand an island that does not exist.
    """
    out = []
    for m in (mocks or []):
        dt = _parse_dt(m.get("finished"))
        if dt and start <= dt.astimezone(tz).date() <= end:
            out.append(m)
    return out


def _pace(total: int, today, week_to_date: int, done_today: int,
          unit: str = "islands") -> dict[str, Any]:
    """Today's expectation, derived from THIS week's target.

    Whatever was still outstanding when today began, spread over the days the week has
    left (today included) and rounded up. So the daily number follows the weekly one
    whenever the parent changes it, a day missed early is pushed into the days that
    remain rather than quietly written off, and a week already finished asks for
    nothing more.

    Measured from the START of today on purpose. Dividing the *live* remainder by the
    days left would lower the bar with every island he finished, so a Monday target of
    21 could be "met" with two.
    """
    days_left = 7 - today.weekday()                      # today … Sunday, always >= 1
    before_today = max(0, week_to_date - done_today)
    outstanding = max(0, total - before_today)
    due_today = -(-outstanding // days_left)             # ceil, integer-only
    still = max(0, due_today - done_today)
    return {
        "weekly_islands": total,
        "day_name": today.strftime("%A"),
        "day_index_mon_is_1": today.weekday() + 1,
        "days_left_including_today": days_left,
        "completed_before_today": before_today,
        "completed_today": done_today,
        "completed_week_to_date": before_today + done_today,
        "outstanding_at_start_of_today": outstanding,
        "due_today": due_today,
        "still_to_do_today": still,
        "met_today": done_today >= due_today,
        "week_target_met": before_today + done_today >= total,
        "explain": (f"This week's target is {total} {unit}. {before_today} were done "
                    f"before today, leaving {outstanding} across {days_left} day"
                    f"{'' if days_left == 1 else 's'} → {due_today} due today; "
                    f"{done_today} done, {still} still to do."),
    }


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
                # Do NOT fall back to fixtures here. This used to look like a successful
                # run carrying sample data ("5 islands all-time" against a real 403), and
                # a dynamic rule acted on it — keeping streaming blocked after Luke had
                # actually done his work. A failed live fetch is a failure, and the last
                # known-good state stays untouched.
                return CollectResult(
                    ok=False, live=False,
                    detail=(f"live fetch failed ({type(e).__name__}: {e}) — kept the "
                            f"previous data rather than substituting fixtures"))
        if ctx.live:
            # live was asked for but authenticate() could not establish a session
            return CollectResult(
                ok=False, live=False,
                detail=(session.get("warning")
                        or "no live Atom session; refusing to pass fixtures off as real"))
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
            # Keep the domain EXACTLY as stored, leading dot and all. Stripping it filed
            # our cookie under "atomlearning.com" while the server sets ".atomlearning.com",
            # so the jar held two atom-auth cookies and the refreshed one never replaced
            # the stale one — the session then died on its 2-day clock however often we
            # polled, which is the opposite of the intended sliding window.
            jar.set(c["name"], c.get("value", ""),
                    domain=(c.get("domain") or ""), path=c.get("path", "/"))
        return httpx.AsyncClient(base_url=api_base, cookies=jar, headers=_UA,
                                 timeout=20.0, follow_redirects=True)

    async def _touch_session(self, client) -> Optional[dict[str, Any]]:
        """GET /ms_accounts/user — the ONLY endpoint that re-issues the sliding
        ``atom-auth`` cookie.

        Measured: of the five endpoints a collect uses, this is the sole one that sends
        a fresh Set-Cookie; islands, learning-resources, score and mock_tests never do.
        So it has to be called on EVERY live collect. It used to be skipped whenever
        ``student_id`` was configured — a sensible-looking optimisation that quietly
        capped the session at ~2 days from the last manual sign-in however often Warden
        polled, which is exactly the "capture once, runs forever" promise it broke.
        """
        r = await client.get("/ms_accounts/user")
        if r.status_code in (401, 403):
            raise RuntimeError("session expired (401/403) — re-run atom login")
        if r.status_code >= 400:
            return None
        try:
            return r.json()
        except Exception:
            return None

    async def _resolve_student(self, client, ctx: SourceContext,
                               user: Optional[dict[str, Any]] = None) -> str:
        configured = ctx.options.get("student_id")
        if configured:
            return str(configured)
        if user is None:
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
            # first, and unconditionally — this is what keeps the session alive
            user = await self._touch_session(client)
            sid = await self._resolve_student(client, ctx, user)
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
            # This week's plan, as Atom itself publishes it — best-effort, never fatal:
            # a week we cannot read falls back to the last number the hub stored.
            plan = await self._fetch_plan(client, ctx, sid, scores)
            # Atom's atom-auth cookie is a SLIDING session — the API re-issues it on
            # every call. Save the refreshed jar so the session stays alive indefinitely
            # as long as we keep polling (capture-once, runs-forever — even in a container).
            self._persist_refreshed_cookies(client, self._state_path(ctx))
        return self._build(ctx, subject, islands, resources, live=True, scores=scores,
                           mocks=mocks, plan=plan)

    @staticmethod
    async def _get_list(client, path: str,
                        params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        try:
            r = await client.get(path, params=params)
            return _as_list(r.json()) if r.status_code < 400 else []
        except Exception:
            return []

    @staticmethod
    async def _get_json(client, path: str) -> Optional[dict[str, Any]]:
        try:
            r = await client.get(path)
            return r.json() if r.status_code < 400 else None
        except Exception:
            return None

    async def _fetch_plan(self, client, ctx: SourceContext, sid: str,
                          scores: Optional[list] = None) -> Optional[dict[str, Any]]:
        """This week's workload as ATOM publishes it — the number on Luke's dashboard.

        `learning-journey-targets` answers "how much is he meant to do this week?" per
        subject, for his year group on his course, and it moves every week (17 in the
        week of 31 Aug 2026; 20 islands + 1 mock test the week after). The dashboard's
        "islands done" counters divide by exactly these numbers, so reading them here is
        what stops the parent copying the week's figure into Warden by hand every Monday.

        Year group and course come from the account (`baseYearGroup`, the active
        enrolment) unless pinned in options. Anything we cannot resolve means "no plan"
        rather than a guess — the caller then keeps the stored number.
        """
        today = datetime.now(_tz(ctx)).date()
        year_group = ctx.options.get("year_group")
        id_course = ctx.options.get("id_course")
        if year_group is None:
            profile = await self._get_json(client, f"/ms_accounts/students/{sid}")
            year_group = (profile or {}).get("baseYearGroup")
        if id_course is None:
            enrolments = await self._get_list(
                client, f"/ms_subscriptions/students/{sid}/enrolments")
            active = [e for e in enrolments if e.get("status") == "active"] or enrolments
            id_course = next((e.get("id_course") for e in active
                              if e.get("id_course") is not None), None)
        if year_group is None or id_course is None:
            log.warning("atom: could not resolve year group / course for %s — "
                        "keeping the stored weekly target", sid)
            return None

        rows = await self._get_list(
            client, f"/ms_learning/students/{sid}/learning-journey-targets",
            params={"id_course": id_course, "yearGroup": year_group})
        week_no, iso_week = _iso_week(today)
        # The endpoint answers for the current week; keep only rows that say so, so a
        # shifted or cached reply can never be presented as this week's plan.
        rows = [r for r in rows if _as_int(r.get("isoWeek")) == week_no]
        if not rows:
            log.warning("atom: no learning-journey targets for %s (course %s, year %s) — "
                        "keeping the stored weekly target", iso_week, id_course, year_group)
            return None

        names = {r.get("id_subject"): r.get("subjectTitle") for r in (scores or [])
                 if r.get("hierarchyLevel") == "SUBJECT" and r.get("subjectTitle")}
        by_subject: dict[str, dict[str, int]] = {}
        for r in rows:
            key = str(names.get(r.get("id_course_subject"), r.get("id_course_subject")))
            entry = by_subject.setdefault(key, {"islands": 0, "mock_tests": 0})
            entry["islands"] += _as_int(r.get("target")) or 0
            entry["mock_tests"] += _as_int(r.get("targetMockTest")) or 0
        islands = sum(e["islands"] for e in by_subject.values())
        mock_tests = sum(e["mock_tests"] for e in by_subject.values())
        return {
            "total": islands + mock_tests,
            "islands": islands,
            "mock_tests": mock_tests,
            "iso_week": iso_week,
            "year_group": year_group,
            "id_course": id_course,
            "by_subject": by_subject,
            "note": ("Atom's own plan for this ISO week (learning-journey-targets) — the "
                     "figure his dashboard counts against. A mock test is one item of the "
                     "plan, which is why the total can exceed the island count."),
        }

    @staticmethod
    def _persist_refreshed_cookies(client, state_path: Path) -> None:
        """Write the refreshed atomlearning cookies (esp. the sliding atom-auth) back
        into the storage-state file, so the next run uses the extended session.

        Picks the FRESHEST cookie per name. The jar can legitimately hold more than one
        entry under a name (different domain or path), and matching on name alone meant
        an older duplicate could overwrite the token the server had just re-issued —
        silently cancelling the refresh and letting the session expire.
        """
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        cookies = state.get("cookies")
        if not isinstance(cookies, list):
            return

        freshest: dict[str, Any] = {}
        for ck in client.cookies.jar:                       # http.cookiejar.Cookie objects
            if "atomlearning.com" not in (ck.domain or ""):
                continue
            prev = freshest.get(ck.name)
            # a session cookie (expires=None) must never beat a dated one
            if prev is None or (ck.expires or 0) >= (prev.expires or 0):
                freshest[ck.name] = ck

        by_name: dict[str, dict[str, Any]] = {c.get("name"): c for c in cookies}
        changed = False
        for name, ck in freshest.items():
            cur = by_name.get(name)
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
                log.debug("refreshed atom session persisted to %s", state_path)
            except Exception as e:
                # Never silent: if this write fails the session quietly stops sliding and
                # dies days later, which is exactly the failure that was hard to find.
                log.error("could not persist refreshed Atom session to %s: %s: %s",
                          state_path, type(e).__name__, e)
        else:
            log.debug("atom session unchanged after collect (nothing to persist)")

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
               scores: Optional[list] = None, mocks: Optional[list] = None,
               plan: Optional[dict[str, Any]] = None) -> CollectResult:
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

        # What counts as "did his learning" TODAY. The weekly number is Atom's own and
        # moves week to week, so the daily bar is derived from it (see _pace) rather than
        # fixed; daily_target_islands is only the fallback for a week with no target at
        # all. A mock test set for the week counts as one item of it, as Atom counts it.
        week_start = today - timedelta(days=today.weekday())           # Monday
        completed_week = [
            i for i in completed
            if (dt := _parse_dt(i["dateCompleted"]))
            and week_start <= dt.astimezone(tz).date() <= today
        ]
        mocks_week = _mocks_finished(mocks, tz, week_start, today)
        mocks_today = _mocks_finished(mocks, tz, today, today)
        done_week = len(completed_week) + len(mocks_week)
        done_today = len(completed_today) + len(mocks_today)
        weekly, origin = _resolve_target(ctx, plan)
        unit = ("items (islands + mock tests)" if (plan or {}).get("mock_tests")
                else "islands")
        pace = (_pace(weekly, today, done_week, done_today, unit=unit)
                if weekly is not None else None)
        due_today = (pace["due_today"] if pace is not None
                     else int(ctx.options.get("daily_target_islands", 1)))
        require_done = bool(ctx.options.get("require_assignments_done", True))
        daily_complete = (done_today >= due_today
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
                   meta={"completed_today": done_today,
                         "islands_completed_today": len(completed_today),
                         "mock_tests_completed_today": len(mocks_today),
                         "islands_due_today": due_today,
                         "weekly_target": weekly,
                         "weekly_target_origin": origin,
                         "completed_week_to_date": done_week,
                         "assignments_outstanding": len(outstanding)}),
            Signal(key=f"atom.{subject}.islands_completed_today", value=len(completed_today),
                   type=SignalType.number, source="atom", subject=subject),
            # The bar the daily_complete signal is judged against today — published so
            # the UI, the audit trail and a rule can all see WHY today asked for three
            # islands and last Tuesday asked for two.
            Signal(key=f"atom.{subject}.islands_due_today", value=due_today,
                   type=SignalType.number, source="atom", subject=subject,
                   meta={"weekly_target": weekly,
                         "weekly_target_origin": origin,
                         "weekly_plan_published_by_atom": plan,
                         "basis": (pace["explain"] if pace is not None else
                                   "no weekly target set — fixed daily_target_islands")}),
            Signal(key=f"atom.{subject}.assignments_outstanding", value=len(outstanding),
                   type=SignalType.number, source="atom", subject=subject),
        ]

        cursor = last.isoformat() if last else ctx.since_cursor
        mode = "live" if live else "fixtures"
        wk = (f"{done_week}/{weekly} this week" if weekly is not None
              else f"{done_week} this week (no target set)")
        if weekly is not None and origin == "atom-published":
            wk += " (Atom's plan)" if plan else " (Atom's plan, last known)"
        elif weekly is not None and origin == "override":
            wk += " (your target)"
        detail = (f"{mode}: {done_today}/{due_today} due today, {wk}, "
                  f"{len(outstanding)} set-work outstanding")
        if warning:
            detail += f" — {warning}"
        state = self._state_of_play(ctx, islands, resources, scores, mocks, plan)
        state["data_quality"] = {
            "live": live,
            "collected_at": utcnow().isoformat(),
            "note": ("real data from Atom" if live else
                     "SAMPLE DATA — offline fixtures, not Luke's real progress"),
        }
        return CollectResult(ok=True, live=live, items=items, signals=signals,
                             next_cursor=cursor, detail=detail, state=state)

    # ── "state of play" for the dynamic LLM evaluator ────────────────────────
    _SESSION_CAP_MIN = 45.0        # a practice session left open shouldn't count as hours

    @staticmethod
    def _target_block(ctx: SourceContext, week_start, today, completed: int,
                      completed_today: int, plan: Optional[dict[str, Any]] = None,
                      mocks_week: int = 0, mocks_today: int = 0) -> dict[str, Any]:
        """This week's target, today's share of it, and the facts behind both.

        The weekly number is Atom's own plan for this ISO week (learning-journey-targets)
        — the figure Luke's dashboard counts against, and it changes every week. A parent
        override for the week beats it; the configured default is only for a plan we could
        not read. Today's share is derived from whichever won rather than being fixed. A
        mock test set for the week is one item of that plan (Atom counts it inside the
        subject's "islands done" tally), so it counts here too — in the target, and in
        what has been done. `todays_requirement` is the very number the
        atom.<subject>.daily_complete signal is judged against, so the state a rule reads
        and the signal a rule may be gated by cannot drift apart.

        The split is still a CONVENIENCE for a rule that wants it, not the answer.
        Measured over 21 evaluations, the model computed custom shapes (a tenth of the
        target, four fifths across weekdays, one per elapsed day minus three) correctly
        every time — including cases whose verdict contradicted the even split — and it
        did so whether or not it was told to ignore these fields. So the arithmetic is
        safe to leave to the rule, and precomputing one policy here must not be allowed
        to override a rule that defines a different one. Hence the explicit labels.
        """
        fallback = int(ctx.options.get("daily_target_islands", 1))
        total, origin = _resolve_target(ctx, plan)
        units, units_today = completed + mocks_week, completed_today + mocks_today
        if total is None:
            return {
                "weekly_islands": None,
                "note": ("no weekly target set — a rule needing one cannot judge it; "
                         "Warden falls back to a fixed daily minimum"),
                "todays_requirement": {
                    "islands_due_today": fallback,
                    "still_to_do_today": max(0, fallback - completed_today),
                    "basis": "fixed daily_target_islands option — no weekly target set",
                },
            }

        unit = "items (islands + mock tests)" if (plan or {}).get("mock_tests") else "islands"
        pace = _pace(total, today, units, units_today, unit=unit)
        names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday"]
        # round-half-up, cumulative, so day 7 always lands exactly on the total
        cumulative = {names[i]: int(total * (i + 1) / 7 + 0.5) for i in range(7)}
        due_by_today = cumulative[names[today.weekday()]]
        return {
            # ── the facts: judge the rule from these ──────────────────────────
            "weekly_islands": total,
            "origin": origin,
            "origin_note": {
                "atom-published": (
                    "Atom's own plan for this week, read live from his account rather "
                    "than typed in — it changes weekly" if plan else
                    "Atom's plan for this week as of the last run that could read it — "
                    "this run could not, so the stored number is being used"),
                "override": "a target the parent set for this week; it beats Atom's plan",
                "config-default": ("fallback — Atom's plan could not be read this run, so "
                                   "the last number Warden held is being used"),
            }.get(origin, ""),
            "published_by_atom": plan,
            "week_starts_monday": str(week_start),
            "day_index_mon_is_1": today.weekday() + 1,
            "day_name": names[today.weekday()],
            "completed_week_to_date": units,
            "islands_completed_week_to_date": completed,
            "mock_tests_completed_week_to_date": mocks_week,
            "completed_today": units_today,
            # ── what Warden asks of today, derived from THIS week's number ────
            "todays_requirement": {
                "islands_due_today": pace["due_today"],
                "still_to_do_today": pace["still_to_do_today"],
                "met": pace["met_today"],
                "week_target_met": pace["week_target_met"],
                "days_left_including_today": pace["days_left_including_today"],
                "basis": ("what was left of the weekly target at the start of today, "
                          "spread over the days the week has left (today included) and "
                          "rounded up — so it moves with the weekly number, and a day "
                          "missed early raises the days that follow"),
                "note": ("This is the shape the atom.<subject>.daily_complete signal "
                         "enforces. If THIS rule defines a different one (weekdays only, "
                         "a fraction by a given day, more at weekends), work it out from "
                         "weekly_islands and completed_week_to_date and ignore this."),
                "explain": pace["explain"],
            },
            # ── is the week on pace at all? even spread, cumulative ───────────
            "even_pace_reference": {
                "assumes": ("the weekly target spread evenly across all seven days, "
                            "rounded half up — a second opinion, not the requirement"),
                "cumulative_due_by_day": cumulative,
                "due_by_end_of_today": due_by_today,
                "on_track": units >= due_by_today,
                "shortfall_today": max(0, due_by_today - units),
                "explain": (f"On an even split, by the end of {names[today.weekday()]} he "
                            f"needs {due_by_today} of the week's {total}; he has "
                            f"{units}."),
            },
        }

    def _state_of_play(self, ctx: SourceContext, islands: list[dict[str, Any]],
                       resources: list[dict[str, Any]], scores: Optional[list] = None,
                       mocks: Optional[list] = None,
                       plan: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        tz = _tz(ctx)
        now = datetime.now(tz)
        today = now.date()

        # subject id -> name (from the score endpoint) so islands read nicely
        subj_names: dict[Any, str] = {}
        for row in (scores or []):
            if row.get("id_subject") is not None and row.get("subjectTitle"):
                subj_names[row["id_subject"]] = row["subjectTitle"]

        per_day: dict[Any, dict[str, Any]] = {}

        def day_entry(d: Any) -> dict[str, Any]:
            return per_day.setdefault(d, {"minutes": 0.0, "islands": []})

        for isl in islands:
            if isl.get("status") != "completed":
                continue
            isl_minutes = 0.0
            for s in (isl.get("sessions") or []):
                st, cp = _parse_dt(s.get("started")), _parse_dt(s.get("completed"))
                if not (st and cp):
                    continue
                mins = (cp - st).total_seconds() / 60.0
                if mins <= 0:
                    continue
                mins = round(min(mins, self._SESSION_CAP_MIN), 1)
                # minutes land on the day they were actually worked
                e = day_entry(cp.astimezone(tz).date())
                e["minutes"] = round(e["minutes"] + mins, 1)
                isl_minutes += mins
            # An island COUNTS on the day Atom says it was completed — the same fact the
            # islands_completed_today signal and the weekly target are counted from, so
            # the state and the signals cannot disagree (a session running past midnight,
            # or an island with no session record at all, used to make them). Atom's own
            # word for a unit of work is an "island", so a rule written in Atom's
            # vocabulary needs no translation step either.
            done = _parse_dt(isl.get("dateCompleted"))
            if done is None:
                continue
            e = day_entry(done.astimezone(tz).date())
            e["islands"].append({"title": isl.get("title"),
                                 "subject": subj_names.get(isl.get("id_course_subject"),
                                                           isl.get("id_course_subject")),
                                 "minutes": round(isl_minutes, 1),
                                 "at": done.astimezone(tz).strftime("%H:%M")})
        days = sorted(per_day, reverse=True)
        last = days[0] if days else None
        assignments = [
            {"name": r.get("name"), "type": r.get("type"),
             "started": bool(r.get("started")), "due": r.get("dueDate"),
             "set_by": (r.get("setBy") or {}).get("name")}
            for r in resources if (r.get("setBy") or {}).get("roles", {}).get("prnt")
        ]

        # Week-to-date, Monday-anchored. Rules commonly set a weekly island target with a
        # pro-rata daily expectation, so hand over the running total and the day index
        # rather than making the model work out where the week started and add it up.
        week_start = today - timedelta(days=today.weekday())      # Monday
        week_days = [d for d in per_day if week_start <= d <= today]
        wk_islands = sum(len(per_day[d]["islands"]) for d in week_days)
        wk_minutes = round(sum(per_day[d]["minutes"] for d in week_days), 1)
        today_entry = per_day.get(today, {"minutes": 0.0, "islands": []})
        mocks_week = _mocks_finished(mocks, tz, week_start, today)
        mocks_today = _mocks_finished(mocks, tz, today, today)
        target = self._target_block(ctx, week_start, today, wk_islands,
                                    len(today_entry["islands"]), plan,
                                    len(mocks_week), len(mocks_today))
        requirement = target.get("todays_requirement") or {}

        state: dict[str, Any] = {
            "source": "atom", "subject": ctx.subject or "luke",
            "now": now.strftime("%Y-%m-%d %H:%M (%A)"),
            # what today asks for, next to what today has actually done
            "today": {"date": str(today), **today_entry,
                      "islands_due_today": requirement.get("islands_due_today"),
                      "still_to_do_today": requirement.get("still_to_do_today")},
            "last_active_day": ({"date": str(last), **per_day[last]} if last else None),
            "recent_days_minutes": {str(d): per_day[d]["minutes"] for d in days[:7]},
            "week_to_date": {
                "week_starts_monday": str(week_start),
                "day_of_week": today.strftime("%A"),
                "day_index_mon_is_1": today.weekday() + 1,
                "islands_completed": wk_islands,
                "mock_tests_completed": len(mocks_week),
                # the target is measured in plan items: Atom's plan counts a mock test as
                # one of them, so the week's tally does too
                "plan_items_completed": wk_islands + len(mocks_week),
                "minutes": wk_minutes,
                "by_day": {str(d): {"islands": len(per_day[d]["islands"]),
                                    "minutes": per_day[d]["minutes"]}
                           for d in sorted(week_days)},
                "target": target,
            },
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
