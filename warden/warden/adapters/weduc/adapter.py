"""Weduc / ReachMoreParents adapter — the school-portal source.

API-first, session-cookie auth — same shape as the Atom adapter.

The portal is a **Blazor WebAssembly** app served from ``ui.app.weduc.co.uk``, but its
data comes from a separate REST host, ``app.weduc.co.uk`` (the SPA reads that from its
own ``/appsettings.json`` → ``BaseUrl``). Hitting the UI host for API paths just returns
the SPA shell, which is the trap this adapter exists to avoid.

Auth is a plain ``PHPSESSID`` cookie on ``app.weduc.co.uk`` — no bearer token (verified:
0 of 832 recorded XHRs sent an Authorization header). So we capture the session once
with Playwright (``python -m warden.adapters.weduc.login``) and replay it headlessly
with httpx thereafter — no browser at collect time, so the container needs none.

Unlike Atom (Google SSO, human required), Weduc's login is username+password, so when
the session dies this adapter can **re-capture by itself** from the secrets in .env.

Endpoints used (verified live 26 Jul 2026; dinner 3 Sep 2026):
  GET  /forms/form/list/target_user/{child}/offset/0/entity/{entity}   outstanding forms
  GET  /user/profile/get/user/{id}                                     profile/attendance
  GET  /calendar/index/props                                           calendar config
  POST /dashboard/river/get/user/{user}/offset/0                       newsfeed
  POST /message/message/list                                           messages
  POST /rest/dinner/getEvents                                          school lunches

Every response is wrapped in either ``{Status, Message, Body}`` or ``{Header:{Status},
Body}`` — :func:`_body` unwraps both.

Mechanical only (contract §2): fetch → normalise → Item[], with faithful
``audience_tags`` and ``subject_ids``. Relevance/summarisation are the hub's job.
Derived signals: ``weduc.<subject>.forms_outstanding`` (number),
``weduc.<subject>.school_day_today`` (bool), ``weduc.<subject>.lunch_booked_tomorrow``
(bool).

Falls back to fixtures/ whenever live is unavailable, so the app stays demoable offline.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from ...models import Attachment, Item, Signal, SignalType, utcnow
from ..base import (
    AdapterManifest, CollectResult, HealthResult, SourceAdapter, SourceContext,
)

_HERE = Path(__file__).resolve().parent
_MANIFEST_PATH = _HERE / "manifest.json"
_REPO_ROOT = _HERE.parents[2]                      # …/warden

API_BASE = "https://app.weduc.co.uk"
UI_BASE = "https://ui.app.weduc.co.uk"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    # the API host is not the page origin; send what the SPA sends
    "Origin": UI_BASE,
    "Referer": UI_BASE + "/",
    "X-Requested-With": "XMLHttpRequest",
}


# ── helpers ──────────────────────────────────────────────────────────────────
def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_dt(s: Any) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            if fmt is None:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def _body(payload: Any) -> Any:
    """Unwrap Weduc's two envelope shapes: {Status,Message,Body} and {Header,Body}."""
    if not isinstance(payload, dict):
        return payload
    if "Body" in payload:
        return payload["Body"]
    return payload


def _ok(payload: Any) -> bool:
    """Did this envelope succeed?

    An explicit Status wins. Weduc's ERROR envelopes still carry a ``Body`` key
    (``{"Header":{"Status":"Error"},"Body":[]}``), so treating "Body exists" as success
    made a dead session look alive — the adapter then skipped its automatic re-login and
    fell back to fixtures instead of healing itself.
    """
    if not isinstance(payload, dict):
        return False
    status = payload.get("Status") or (payload.get("Header") or {}).get("Status")
    if status is not None:
        return str(status).strip().lower() in ("success", "ok", "200")
    return "Body" in payload


class Adapter(SourceAdapter):
    manifest: AdapterManifest = AdapterManifest.model_validate_json(
        _MANIFEST_PATH.read_text(encoding="utf-8")
    )

    # ── contract ops ─────────────────────────────────────────────────────────
    async def authenticate(self, ctx: SourceContext) -> dict[str, Any]:
        if not ctx.live:
            return {"mode": "fixtures"}

        state = self._state_path(ctx)
        if await self._session_alive(ctx):
            return {"mode": "live", "state_path": str(state)}

        # Weduc logs in with a username+password, so — unlike Atom — we can refresh
        # the session unattended rather than asking a human to sign in again.
        if ctx.has_secrets:
            ok, detail = await self._recapture(ctx)
            if ok and await self._session_alive(ctx):
                return {"mode": "live", "state_path": str(state), "note": "session re-captured"}
            return {"mode": "fixtures",
                    "warning": f"could not refresh the Weduc session ({detail}); used fixtures"}

        return {"mode": "fixtures",
                "warning": (f"no valid Weduc session at {state} and no WEDUC_USERNAME/"
                            "WEDUC_PASSWORD to make one — used fixtures")}

    async def collect(self, ctx: SourceContext, session: dict[str, Any]) -> CollectResult:
        subject = ctx.subject or "luke"
        if session.get("mode") == "live":
            try:
                return await self._collect_live(ctx, subject)
            except Exception as e:
                # A failed live fetch must not masquerade as data — see the note in the
                # Atom adapter. Keep the last known-good state instead.
                return CollectResult(
                    ok=False, live=False,
                    detail=(f"live fetch failed ({type(e).__name__}: {e}) — kept the "
                            f"previous data rather than substituting fixtures"))
        if ctx.live:
            return CollectResult(
                ok=False, live=False,
                detail=(session.get("warning")
                        or "no live Weduc session; refusing to pass fixtures off as real"))
        return self._collect_fixtures(ctx, subject, warning=session.get("warning", ""))

    async def healthcheck(self, ctx: SourceContext) -> HealthResult:
        if ctx.live:
            if await self._session_alive(ctx):
                return HealthResult(ok=True, detail="live session valid")
            has = "creds present — will self-refresh" if ctx.has_secrets else "no creds"
            return HealthResult(ok=False, detail=f"session expired/absent ({has})")
        p = ctx.fixtures_path()
        ok = bool(p and (p / "snapshot.json").exists())
        return HealthResult(ok=ok, detail="fixtures present" if ok else "snapshot.json missing")

    # ── session plumbing ─────────────────────────────────────────────────────
    def _state_path(self, ctx: SourceContext) -> Path:
        raw = ctx.options.get("state_file") or "data/weduc_state.json"
        p = Path(raw)
        return p if p.is_absolute() else (_REPO_ROOT / p)

    def _api_base(self, ctx: SourceContext) -> str:
        return str(ctx.options.get("api_base", API_BASE)).rstrip("/")

    @staticmethod
    def _load_cookies(state_path: Path) -> list[dict[str, Any]]:
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        cookies = data.get("cookies") if isinstance(data, dict) else None
        if not isinstance(cookies, list):
            return []
        return [c for c in cookies if "weduc" in (c.get("domain") or "")]

    def _client(self, ctx: SourceContext):
        import httpx
        jar = httpx.Cookies()
        for c in self._load_cookies(self._state_path(ctx)):
            # Domain kept exactly as stored — stripping the leading dot files our copy
            # separately from the server's, producing duplicate cookies whose refresh
            # then fights itself (see the note in the Atom adapter).
            jar.set(c["name"], c.get("value", ""),
                    domain=(c.get("domain") or ""), path=c.get("path", "/"))
        return httpx.AsyncClient(base_url=self._api_base(ctx), cookies=jar,
                                 headers=_HEADERS, timeout=25.0, follow_redirects=True)

    async def _session_alive(self, ctx: SourceContext) -> bool:
        """A dead session still answers 200 — with the SPA shell — so check for JSON,
        not for a status code."""
        if not self._load_cookies(self._state_path(ctx)):
            return False
        try:
            async with self._client(ctx) as client:
                r = await client.get(f"/user/profile/get/user/{self._user_id(ctx)}")
            if "json" not in r.headers.get("content-type", "").lower():
                return False
            return _ok(r.json())
        except Exception:
            return False

    async def _recapture(self, ctx: SourceContext) -> tuple[bool, str]:
        """Re-run the Playwright login helper to mint a fresh session."""
        try:
            from .login import capture_session
        except Exception as e:
            return False, f"login helper unavailable: {type(e).__name__}"
        try:
            return await capture_session(
                state_file=self._state_path(ctx),
                username=ctx.secrets.get("username", ""),
                password=ctx.secrets.get("password", ""),
                ui_base=str(ctx.options.get("ui_base", UI_BASE)).rstrip("/"),
            )
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    # ── ids (configurable, with the values observed on this account) ─────────
    @staticmethod
    def _user_id(ctx: SourceContext) -> str:
        return str(ctx.options.get("user_id") or "281474978315305")

    @staticmethod
    def _child_id(ctx: SourceContext) -> str:
        return str(ctx.options.get("child_id") or "281474978315151")

    @staticmethod
    def _entity_id(ctx: SourceContext) -> str:
        return str(ctx.options.get("entity_id") or "281474976822310")

    # ── live collection ──────────────────────────────────────────────────────
    @staticmethod
    async def _get_json(client, method: str, path: str, **kw) -> Any:
        """Fetch and unwrap, tolerating the SPA-shell response. Never raises.

        Content-type is NOT trustworthy here: /calendar/event/fetch returns a perfectly
        good JSON array labelled ``text/html``. So we detect the SPA shell by its leading
        markup and otherwise just try to parse.
        """
        try:
            r = await client.request(method, path, **kw)
            text = r.text.lstrip()
            if not text or text[0] not in "[{":       # the SPA shell, or an error page
                return None
            payload = json.loads(text)
            if isinstance(payload, list):             # bare array (calendar events)
                return payload
            return _body(payload) if _ok(payload) else None
        except Exception:
            return None

    async def _collect_live(self, ctx: SourceContext, subject: str) -> CollectResult:
        child, entity, user = self._child_id(ctx), self._entity_id(ctx), self._user_id(ctx)
        async with self._client(ctx) as client:
            forms_body = await self._get_json(
                client, "GET",
                f"/forms/form/list/target_user/{child}/offset/0/entity/{entity}")
            if forms_body is None:
                raise RuntimeError("forms endpoint returned no JSON — session likely expired")

            profile = await self._get_json(client, "GET", f"/user/profile/get/user/{child}")
            # These two only answer with JSON when given the form-encoded params the SPA
            # sends; without them the API politely returns the SPA shell instead.
            river = await self._get_json(
                client, "POST", f"/dashboard/river/get/user/{user}/offset/0",
                data={"user": user, "start_date": "", "end_date": "",
                      "status": "0", "custom_post_type": "1"})
            messages = await self._get_json(
                client, "POST", "/message/message/list",
                data={"start": "0", "search": "",
                      "folder": str(ctx.options.get("inbox_folder_id") or "281474986227878")})
            # School calendar: term boundaries, INSET closures and dated events. This is
            # what lets a rule say "no need to check while he's at school".
            # A YEAR back, not a fortnight. Term boundaries are what tell us whether
            # school is actually on — term *periods* run to the end of their month, so
            # Summer Term still "contains" all of August. With a short lookback the last
            # "End of Term" event scrolls out of view, no prior boundary is found, the
            # period fallback wins, and mid-August reads as a school day. That then makes
            # a rule take its first look at 4pm instead of 8am.
            back = int(ctx.options.get("calendar_days_back", 365))
            fwd = int(ctx.options.get("calendar_days_ahead", 120))
            today = datetime.now(self._tz(ctx)).date()
            events = await self._get_json(
                client, "GET", "/calendar/event/fetch",
                params={"start": f"{today - timedelta(days=back)}T00:00:00.000Z",
                        "end": f"{today + timedelta(days=fwd)}T23:59:59.000Z"}) or []
            # School lunches: what he's having, and — the useful part — which school
            # days have nothing ordered at all.
            meals = await self._collect_meals(client, ctx, today)
            self._persist_refreshed_cookies(client, self._state_path(ctx))

        items: list[Item] = []
        child_class = self._class_of(profile)

        # ── forms — the endpoint the outstanding-forms signal is built on ────
        # This list is the portal's "Available Forms" tab: everything here is
        # awaiting action, so its length IS the outstanding count.
        form_items = forms_body.get("items", []) if isinstance(forms_body, dict) else []
        outstanding: list[str] = []
        for form in form_items:
            title = str(form.get("title") or "").strip()
            outstanding.append(title)
            items.append(Item(
                source_id="weduc", subject_ids=[subject],
                external_id=f"weduc:form:{form.get('id')}",
                kind="form", title=title,
                body_text=str(form.get("description") or "Awaiting completion"),
                audience_tags=[t for t in [child_class] if t],
                url=f"{UI_BASE}/forms/index/forms/user/{child}",
                raw=form,
            ))

        # ── newsfeed (incl. the newsletter PDFs) ─────────────────────────────
        async with self._client(ctx) as dl:
            for post in self._rows(river, "items"):
                tags = [str(t) for t in (post.get("containers") or [])]
                atts = self._attachments_of(post)
                cached = await self._cache_files(ctx, dl, atts)
                raw = dict(post)
                if cached:
                    # where the hub's document reader picks them up (see warden/documents.py)
                    raw["_files"] = cached
                items.append(Item(
                    source_id="weduc",
                    subject_ids=[subject] if child_class and child_class in tags else [],
                    external_id=f"weduc:post:{post.get('id')}",
                    kind="post",
                    title=str(post.get("title") or ""),
                    body_text=str(post.get("content") or post.get("contentHtml") or ""),
                    occurred_at=_parse_dt(post.get("createdAt_ISO8601")
                                          or post.get("createdAt")),
                    audience_tags=tags,
                    attachments=[
                        Attachment(kind="file", url=a["url"], name=a["name"],
                                   mime=a.get("mime"))
                        for a in atts
                    ],
                    raw=raw,
                ))

        # ── messages ─────────────────────────────────────────────────────────
        for msg in self._rows(messages, "data"):
            items.append(Item(
                source_id="weduc", subject_ids=[],
                external_id=f"weduc:message:{msg.get('id')}",
                kind="message",
                title=str(msg.get("subject") or msg.get("title") or "Message"),
                body_text=str(msg.get("body") or msg.get("preview") or ""),
                occurred_at=_parse_dt(msg.get("created") or msg.get("date")),
                audience_tags=[str(msg.get("type"))] if msg.get("type") else [],
                raw=msg,
            ))

        # ── calendar events (term boundaries, INSET closures, dated events) ──
        for ev in events:
            start = _parse_dt(ev.get("start"))
            items.append(Item(
                source_id="weduc", subject_ids=[],
                external_id=f"weduc:event:{ev.get('id')}",
                kind="calendar_event",
                title=str(ev.get("title") or ""),
                body_text=("All day" if ev.get("allDay") else "") ,
                occurred_at=start,
                audience_tags=["closure"] if self._is_closure(str(ev.get("title") or "")) else [],
                raw=ev,
            ))

        # ── school lunches ───────────────────────────────────────────────────
        for m in meals:
            items.append(Item(
                source_id="weduc", subject_ids=[subject],
                external_id=f"weduc:meal:{m['date']}",
                kind="meal",
                title=m["choice"] or "No meal ordered",
                body_text=f"{m['state_label']}"
                          + (f" — {m['menu']}" if m["menu"] else ""),
                occurred_at=_parse_dt(m["date"]),
                audience_tags=[m["state"]],
                raw=m,
            ))

        school = self._school_context(ctx, events, profile)
        meals_summary = self._meals_summary(meals, school, today)
        tomorrow = str(today + timedelta(days=1))
        meal_tomorrow = next((m for m in meals if m["date"] == tomorrow), None)
        signals = [
            Signal(key=f"weduc.{subject}.forms_outstanding", value=len(outstanding),
                   type=SignalType.number, source="weduc", subject=subject,
                   meta={"forms": outstanding}),
            Signal(key=f"weduc.{subject}.school_day_today", value=school["likely_school_day"],
                   type=SignalType.bool, source="weduc", subject=subject,
                   meta={"term": school["current_term"],
                         "closed_today": school["closed_today"],
                         "weekday": school["weekday"]}),
            Signal(key=f"weduc.{subject}.lunch_booked_tomorrow",
                   value=bool(meal_tomorrow and meal_tomorrow["booked"]),
                   type=SignalType.bool, source="weduc", subject=subject,
                   meta={"date": tomorrow,
                         "choice": (meal_tomorrow or {}).get("choice", ""),
                         "next_unbooked": meals_summary["next_unbooked"]}),
        ]
        detail = (f"live: {len(items)} items ({len(form_items)} forms, "
                  f"{len(self._rows(river, 'items'))} posts, "
                  f"{len(self._rows(messages, 'data'))} messages, {len(events)} events, "
                  f"{len(meals)} meal days), "
                  f"{len(outstanding)} forms outstanding, "
                  f"school_day={school['likely_school_day']}, "
                  f"{len(meals_summary['unbooked_school_days'])} school day(s) with no lunch")
        state = self._state_of_play(subject, child_class, outstanding, profile,
                                    school=school, meals=meals_summary)
        state["data_quality"] = {"live": True, "collected_at": utcnow().isoformat(),
                                 "note": "real data from Weduc"}
        return CollectResult(
            ok=True, live=True, items=items, signals=signals,
            next_cursor=utcnow().isoformat(), detail=detail, state=state,
        )

    # ── attachments (the newsletter lives here) ──────────────────────────────
    _MIME = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg",
             "jpeg": "image/jpeg", "doc": "application/msword",
             "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}

    @classmethod
    def _attachments_of(cls, post: dict[str, Any]) -> list[dict[str, Any]]:
        """Files hang off ``postType.items[]`` — there is no ``attachments`` key.

        This is where the weekly newsletter PDF lives, and it carries per-class and
        per-child content that appears nowhere else in the portal.
        """
        out: list[dict[str, Any]] = []
        pt = post.get("postType")
        entries = (pt.get("items") if isinstance(pt, dict) else None) or []
        for a in entries:
            if not isinstance(a, dict):
                continue
            url = a.get("external_url") or a.get("url")
            if not url:
                continue
            ext = str(a.get("extension") or "").lower().lstrip(".")
            out.append({"url": str(url), "name": str(a.get("title") or "attachment"),
                        "ext": ext, "mime": cls._MIME.get(ext),
                        "id": a.get("id")})
        return out

    async def _cache_files(self, ctx: SourceContext, client,
                           atts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Download attachments we don't already have, into data/weduc_files/.

        Keyed on the portal's own attachment **id**, NOT the hash in the download URL.
        That hash looks content-addressed but is a per-session token: it rotates whenever
        the adapter re-authenticates, so keying on it re-downloaded the same 28 MB
        newsletter and paid for a fresh LLM read on every session change. The id is
        stable across sessions.
        """
        wanted = {e.strip().lower() for e in
                  str(ctx.options.get("cache_extensions", "pdf")).split(",") if e.strip()}
        if not wanted:
            return []
        cache = self._files_dir(ctx)
        cache.mkdir(parents=True, exist_ok=True)
        max_mb = float(ctx.options.get("max_attachment_mb", 60))

        out: list[dict[str, Any]] = []
        for a in atts:
            if a["ext"] not in wanted:
                continue
            stem = str(a.get("id") or "") or Path(urlparse(a["url"]).path).stem or "file"
            dest = cache / f"{stem}.{a['ext']}"
            if not dest.exists():
                try:
                    r = await client.get(a["url"], headers={"Accept": "*/*"})
                    if r.status_code >= 400 or not r.content:
                        continue
                    if len(r.content) > max_mb * 1024 * 1024:
                        continue
                    dest.write_bytes(r.content)
                except Exception:
                    continue
            out.append({"name": a["name"], "ext": a["ext"], "mime": a.get("mime"),
                        "path": str(dest), "key": stem, "url": a["url"],
                        "bytes": dest.stat().st_size if dest.exists() else 0})
        return out

    def _files_dir(self, ctx: SourceContext) -> Path:
        raw = ctx.options.get("files_dir") or "data/weduc_files"
        p = Path(raw)
        return p if p.is_absolute() else (_REPO_ROOT / p)

    @staticmethod
    def _rows(body: Any, key: str) -> list[dict[str, Any]]:
        if isinstance(body, dict):
            v = body.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
        return []

    # ── school lunches (/rest/dinner/getEvents) ──────────────────────────────
    # The meals calendar colours each day, and the page's own legend defines what the
    # colours mean. Colour is the mapping the portal itself publishes, so it is what we
    # key on; the numeric `status` rides along unmapped rather than being guessed at
    # (only 3 and 4 have ever been observed, so inventing names for 1 and 2 would be
    # fiction). A day with NO row at all is the real "nothing booked" case.
    _MEAL_STATES: dict[str, tuple[str, str]] = {
        "#30c05b": ("ordered", "Completed order"),
        "#ffb347": ("unpaid", "Unpaid order"),
        "#ff6961": ("incomplete", "Incomplete order"),
        "#ccc": ("no_action", "No action required"),
    }

    async def _collect_meals(self, client, ctx: SourceContext, today) -> list[dict[str, Any]]:
        """One row per day the caterer has a booking/menu for. Never raises."""
        back = int(ctx.options.get("meals_days_back", 7))
        fwd = int(ctx.options.get("meals_days_ahead", 35))
        body = await self._get_json(
            client, "POST", "/rest/dinner/getEvents",
            json={"start_date": f"{today - timedelta(days=back)}T00:00:00+00:00",
                  "end_date": f"{today + timedelta(days=fwd)}T00:00:00+00:00",
                  "entity": int(self._entity_id(ctx)),
                  "target_user": int(self._child_id(ctx))})
        rows: list[dict[str, Any]] = []
        for it in self._rows(body, "items"):
            ep = it.get("extendedProps") or {}
            colour = str(it.get("borderColor") or "").strip().lower()
            state, label = self._MEAL_STATES.get(colour, ("unknown", "Unknown"))
            sels = [str(s).strip() for s in (ep.get("selections") or []) if str(s).strip()]
            rows.append({
                "date": str(it.get("date") or "")[:10],
                "menu": str(ep.get("menu_title") or ""),
                "choices": sels,
                "choice": sels[0] if sels else "",
                "state": state, "state_label": label,
                "status_code": ep.get("status"),
                "booked": bool(sels) and state != "incomplete",
            })
        rows.sort(key=lambda r: r["date"])
        return [r for r in rows if r["date"]]

    @staticmethod
    def _meals_summary(meals: list[dict[str, Any]], school: dict[str, Any],
                       today) -> dict[str, Any]:
        """Join the meal rows onto the school calendar to find the gaps.

        The alerting case is a SCHOOL DAY with no meal row at all — that is what "nothing
        booked" looks like, not a row saying so. Non-school days are skipped, or every
        weekend would read as a missed lunch.
        """
        by_date = {m["date"]: m for m in meals}
        unbooked: list[dict[str, str]] = []
        for d in (school.get("upcoming_days") or []):
            iso = d.get("date", "")
            if not d.get("is_school_day") or iso < str(today):
                continue
            m = by_date.get(iso)
            if m is None:
                unbooked.append({"date": iso, "weekday": d.get("weekday", ""),
                                 "why": "no meal booked"})
            elif not m["booked"]:
                unbooked.append({"date": iso, "weekday": d.get("weekday", ""),
                                 "why": m["state_label"].lower()})
        return {
            "days": meals,
            "unbooked_school_days": unbooked,
            "next_unbooked": unbooked[0] if unbooked else None,
            "derivation": (
                "A school day with no row from /rest/dinner/getEvents has nothing "
                "ordered; a row with choices and a completed/unpaid state is booked. "
                "Only school days are checked, and only from today forward. State names "
                "come from the meals page's own colour legend."),
        }

    # Titles that mean "school is shut". Kept deliberately narrow: a false "closed"
    # would make a rule skip a day it should have checked.
    _CLOSURE_WORDS = ("inset", "school closed", "closed", "bank holiday", "half term",
                      "holiday", "occasional day", "training day")

    @classmethod
    def _is_closure(cls, title: str) -> bool:
        t = (title or "").lower()
        return any(w in t for w in cls._CLOSURE_WORDS)

    @staticmethod
    def _tz(ctx: SourceContext):
        from zoneinfo import ZoneInfo
        try:
            return ZoneInfo(str(ctx.options.get("tz", "Europe/London")))
        except Exception:
            return ZoneInfo("UTC")

    @staticmethod
    def _in_session(day, boundaries: list[dict[str, Any]],
                    terms: list[dict[str, Any]]) -> bool:
        """Is school in session on `day`?

        Prefers the calendar's "End of Term" / "Term N starts" events, which mark the
        real last and first school days. Falls back to term periods, which are only a
        rough guide because they run to the end of their month.
        """
        ds = str(day)
        prior = [b for b in boundaries if b["date"] <= ds]
        if prior:
            return "end" not in prior[-1]["title"].lower()
        # No boundary before this date: fall back to term periods, which are only a rough
        # guide (they run to the end of their month, so they span the holidays). This
        # path should be rare — if it fires often, the calendar lookback is too short.
        return any(t["start"] <= ds <= t["end"] for t in terms)

    @classmethod
    def _school_context(cls, ctx: SourceContext, events: list[dict[str, Any]],
                        profile: Any) -> dict[str, Any]:
        """Facts a rule needs to reason about school days, plus a labelled best guess.

        We hand over the raw inputs (term periods, closure dates, weekday) *and* a
        derived `likely_school_day` — the derivation is stated so the model can
        disagree with it when the data is thin, rather than trusting a bare boolean.
        """
        tz = cls._tz(ctx)
        today = datetime.now(tz).date()

        # Term periods from the child profile. The same list also carries the portal's
        # relative date-range presets ("This Week", "Last 3 Months"), which are NOT terms
        # — including them would let `current_term` resolve to "This Week".
        all_periods: list[dict[str, Any]] = []
        if isinstance(profile, dict):
            for p in (profile.get("periods") or []):
                if not isinstance(p, dict) or p.get("is_default"):
                    continue
                s, e = _parse_dt(p.get("start_date")), _parse_dt(p.get("end_date"))
                if s and e:
                    all_periods.append({"title": str(p.get("title") or ""),
                                        "start": str(s.date()), "end": str(e.date())})
        terms = [p for p in all_periods if "term" in p["title"].lower()]
        if not terms:                      # a school naming terms differently
            terms = all_periods
        in_term = next((t["title"] for t in terms
                        if t["start"] <= str(today) <= t["end"]), None)

        closures, upcoming, boundaries = [], [], []
        for ev in events:
            title = str(ev.get("title") or "")
            start = _parse_dt(ev.get("start"))
            if not start:
                continue
            entry = {"date": str(start.date()), "title": title,
                     "all_day": bool(ev.get("allDay"))}
            if cls._is_closure(title):
                closures.append(entry)
            low = title.lower()
            if "term" in low and any(w in low for w in ("end", "start", "begin")):
                boundaries.append(entry)
            if start.date() >= today:
                upcoming.append(entry)
        for lst in (closures, upcoming, boundaries):
            lst.sort(key=lambda e: e["date"])
        closed_today = [c for c in closures if c["date"] == str(today)]

        # A term PERIOD runs to the end of its month (Summer Term ends 31 Aug), so it
        # spans the holidays and is not evidence school is on. The calendar's
        # "End of Term"/"Term starts" events are the real boundaries — surface them
        # separately and tell the model which to trust.
        #
        # "Are we in the holidays?" is decided by the LAST boundary on or before today:
        # an "end" means the holidays, a "start" means term. It previously asked whether
        # a term start existed *after* today, which is true all year round — so once any
        # end-of-term had passed, every day read as holiday. It went unnoticed through
        # the summer and then called 3 Sep 2026 a holiday, the second day of term, while
        # the day-by-day list (which uses _in_session) correctly said school day.
        last_end = next((b for b in reversed(boundaries)
                         if b["date"] <= str(today) and "end" in b["title"].lower()), None)
        last_boundary = next((b for b in reversed(boundaries)
                              if b["date"] <= str(today)), None)
        next_start = next((b for b in boundaries
                           if b["date"] > str(today) and "end" not in b["title"].lower()), None)
        after_term_end = bool(last_boundary and "end" in last_boundary["title"].lower())

        # A forward-looking day-by-day calendar. Without this the model has to INFER
        # whether some future date is a school day, and it gets it wrong: given only
        # "today is a holiday Sunday" it happily concluded "Monday is a school day"
        # mid-summer-holiday. Turning the question into a lookup removes the guess.
        closure_dates = {c["date"] for c in closures}

        def classify(d) -> tuple[bool, str]:
            wd = d.weekday() < 5
            in_session = cls._in_session(d, boundaries, terms)
            closed = str(d) in closure_dates
            why = ("weekend" if not wd else
                   "school holiday" if not in_session else
                   "closure/INSET" if closed else "school day")
            return bool(wd and in_session and not closed), why

        detail_days = int(ctx.options.get("school_lookahead_days", 21))
        ahead: list[dict[str, Any]] = []
        for n in range(detail_days):
            d = today + timedelta(days=n)
            ok, why = classify(d)
            ahead.append({"date": str(d), "weekday": d.strftime("%a"),
                          "is_school_day": ok, "why": why})

        # Scan well past the detailed window so `next_school_day` is still answerable
        # across a six-week summer holiday, without emitting 60 rows of per-day detail.
        next_school_day = None
        for n in range(int(ctx.options.get("school_scan_days", 120))):
            d = today + timedelta(days=n)
            if classify(d)[0]:
                next_school_day = str(d)
                break

        # Derive the headline from the SAME helper the day-by-day list uses, so the two
        # can never disagree again — the bug above was exactly that divergence.
        is_weekday = today.weekday() < 5
        likely = bool(is_weekday and cls._in_session(today, boundaries, terms)
                      and not closed_today)
        return {
            "next_school_day": next_school_day,
            "upcoming_days": ahead,
            "today": str(today),
            "weekday": today.strftime("%A"),
            "is_weekday": is_weekday,
            "current_term": in_term,
            "in_term_today": bool(in_term),
            "closed_today": closed_today,
            "likely_school_day": likely,
            "last_term_end_seen": last_end,
            "next_term_start": next_start,
            "in_holidays": after_term_end,
            # If no term boundary was found before today we are guessing from term
            # periods, which span the holidays — say so rather than assert a school day.
            "confidence": ("boundary-based" if any(b["date"] <= str(today) for b in boundaries)
                           else "LOW — no term-boundary event before today, falling back "
                                "to term periods which run through the holidays"),
            "derivation": (
                "likely_school_day = is_weekday AND school is in session AND no closure "
                "event today. 'In session' is decided by the most recent term-boundary "
                "event on or before the day: a 'Term starts' means term, an 'End of "
                "Term' means holidays — the same rule that fills upcoming_days, so the "
                "two always agree. CAUTION: a term PERIOD runs to the end of its month "
                "(e.g. Summer Term to 31 Aug) so in_term_today stays true through the "
                "holidays — trust the boundary events over it. Closures are matched on "
                "event titles (INSET, 'school closed', half term, bank holiday), so "
                "unusual wording could be missed. Strong hint, not gospel."),
            "term_periods": terms,
            "term_boundary_events": boundaries[:12],
            "closures_known": closures[:20],
            "upcoming_events": upcoming[:15],
        }

    @staticmethod
    def _class_of(profile: Any) -> Optional[str]:
        """The child's class name, used to attribute class-tagged posts to them.

        Weduc keeps it at ``Body.user.basic.alias`` (verified: "Jellyfish Class") —
        not under any obvious "class" key. Getting this wrong is quiet but costly: the
        class name is what matches a post's audience_tags, so a miss means posts about
        Luke's class never get his subject_id and the hub can't filter to him.
        """
        if not isinstance(profile, dict):
            return None
        basic = ((profile.get("user") or {}).get("basic")
                 if isinstance(profile.get("user"), dict) else None)
        for src in (basic, profile.get("user"), profile):
            if not isinstance(src, dict):
                continue
            for k in ("alias", "class", "className", "form", "registrationGroup"):
                v = src.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return None

    @staticmethod
    def _persist_refreshed_cookies(client, state_path: Path) -> None:
        """PHPSESSID can rotate; write refreshed values back so the session persists."""
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        cookies = state.get("cookies")
        if not isinstance(cookies, list):
            return
        # freshest per name — an older duplicate must not overwrite a re-issued session
        freshest: dict[str, Any] = {}
        for ck in client.cookies.jar:
            if "weduc" not in (ck.domain or ""):
                continue
            prev = freshest.get(ck.name)
            if prev is None or (ck.expires or 0) >= (prev.expires or 0):
                freshest[ck.name] = ck

        by_name = {c.get("name"): c for c in cookies}
        changed = False
        for name, ck in freshest.items():
            cur = by_name.get(name)
            if cur is not None and cur.get("value") != ck.value:
                cur["value"] = ck.value
                if ck.expires:
                    cur["expires"] = ck.expires
                changed = True
        if changed:
            try:
                state_path.write_text(json.dumps(state), encoding="utf-8")
            except Exception:
                pass

    # ── "state of play" for the dynamic LLM evaluator ────────────────────────
    @staticmethod
    def _state_of_play(subject: str, child_class: Optional[str],
                       outstanding: list[str], profile: Any,
                       school: Optional[dict[str, Any]] = None,
                       meals: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        state: dict[str, Any] = {
            "source": "weduc", "subject": subject,
            "class": child_class,
            "forms_outstanding": {"count": len(outstanding), "titles": outstanding},
        }
        if school:
            state["school"] = school
        if meals:
            state["meals"] = meals
        if isinstance(profile, dict) and isinstance(profile.get("attendance"), (dict, list)):
            state["attendance"] = profile["attendance"]
        return state

    # ── fixtures (offline) ───────────────────────────────────────────────────
    def _collect_fixtures(self, ctx: SourceContext, subject: str,
                          warning: str = "") -> CollectResult:
        data: dict[str, Any] = {}
        p = ctx.fixtures_path()
        if p and (p / "snapshot.json").exists():
            data = _read_json(p / "snapshot.json")

        child_class = (data.get("child") or {}).get("class")
        items: list[Item] = []

        for post in data.get("newsfeed", []):
            tags = post.get("audience_tags", [])
            items.append(Item(
                source_id="weduc",
                subject_ids=[subject] if child_class and child_class in tags else [],
                external_id=f"weduc:post:{post['id']}",
                kind="post",
                title=post.get("title", ""),
                body_text=post.get("body", ""),
                occurred_at=_parse_dt(post.get("posted_at")),
                audience_tags=tags,
                url=post.get("url"),
                attachments=[Attachment(**a) for a in post.get("attachments", [])],
                raw=post,
            ))

        outstanding: list[str] = []
        for form in data.get("forms", []):
            if form.get("status") == "outstanding":
                outstanding.append(form.get("title", ""))
            items.append(Item(
                source_id="weduc", subject_ids=[subject],
                external_id=f"weduc:form:{form['id']}",
                kind="form",
                title=form.get("title", ""),
                body_text=f"Status: {form.get('status', '')}",
                due_at=_parse_dt(form.get("due_at")),
                audience_tags=form.get("audience_tags", []),
                url=form.get("url"),
                raw=form,
            ))

        for evt in data.get("calendar", []):
            tags = evt.get("audience_tags", [])
            items.append(Item(
                source_id="weduc",
                subject_ids=[subject] if child_class and child_class in tags else [],
                external_id=f"weduc:event:{evt['id']}",
                kind="calendar_event",
                title=evt.get("title", ""),
                body_text=evt.get("body", ""),
                occurred_at=_parse_dt(evt.get("start")),
                audience_tags=tags,
                url=evt.get("url"),
                raw=evt,
            ))

        # Offline mode still needs school context or a dynamic rule can't reason about
        # cadence; derive it from term dates alone (no calendar events in fixtures).
        school = self._school_context(ctx, [], None)
        signals = [
            Signal(key=f"weduc.{subject}.forms_outstanding", value=len(outstanding),
                   type=SignalType.number, source="weduc", subject=subject,
                   meta={"forms": outstanding}),
            Signal(key=f"weduc.{subject}.school_day_today", value=school["likely_school_day"],
                   type=SignalType.bool, source="weduc", subject=subject,
                   meta={"weekday": school["weekday"], "note": "fixtures: no term data"}),
        ]
        detail = f"fixtures: {len(items)} items, {len(outstanding)} forms outstanding"
        if warning:
            detail += f" — {warning}"
        state = self._state_of_play(subject, child_class, outstanding, None, school=school)
        state["data_quality"] = {
            "live": False, "collected_at": utcnow().isoformat(),
            "note": "SAMPLE DATA — offline fixtures, not the real school portal",
        }
        return CollectResult(
            ok=True, live=False, items=items, signals=signals,
            next_cursor=data.get("captured_at") or utcnow().isoformat(), detail=detail,
            state=state,
        )
