"""ParentPay adapter — is he booked into breakfast club and after-school club?

The school books clubs in ParentPay, not in Weduc; Weduc's Payments page is only a
redirect to it (see login.py for how the SSO gets us there without a second password).

The clubs pages are an Angular app, so the ``.aspx`` URL a parent bookmarks returns an
empty shell — fetching that HTML and parsing it yields nothing. The app fills itself from
a small JSON API, which is what this adapter uses instead:

  GET /clubsbookingApi/internal-details/{club_id}/{consumer_id}
        -> {"internalMemberId": …}   the id the calendar endpoint wants, which is NOT
                                      the ConsumerId in the page URL
  GET /clubsbookingApi/club-booking-calendar/{club_id}/{internal_member_id}
        -> clubName, balance, bookingCutOffSettings, bookingChoices, sessions[]

Each session carries ``status`` (Booked | Available | Unavailable), a capacity and a
current count, and a reason when it is unavailable. So no browser is needed at collect
time — only once, to mint the session.

The **booking cut-off is the whole point**. ``bookingCutOffSettings`` gives it as data
(``DayBeforeTimeBefore``, 1 day, 09:00), so this adapter computes the actual deadline for
each session rather than just repeating the sentence. A reminder that arrives on the
morning of the session is useless — by then it is a day too late.

Mechanical only (contract §2): fetch -> normalise -> Item[] + Signal[]. Whether an
unbooked Tuesday matters is the hub's judgement, not this adapter's.

STRICTLY READ-ONLY. It issues GETs and nothing else. It must never book, cancel or pay
for a session — the API it reads has endpoints that do exactly that, and this code
deliberately never calls them.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from html import unescape
from pathlib import Path
from typing import Any, Optional

from ...models import Item, Signal, SignalType, utcnow
from ..base import (
    AdapterManifest, CollectResult, HealthResult, SourceAdapter, SourceContext,
)

_HERE = Path(__file__).resolve().parent
_MANIFEST_PATH = _HERE / "manifest.json"
_REPO_ROOT = _HERE.parents[2]                      # …/warden

PP_BASE = "https://app.parentpay.com"
API = "/clubsbookingApi"
CAL_PAGE = "/V3Payer4W3/Home/Child/Clubs/ClubsCalendar.aspx"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}

# ParentPay's own status vocabulary -> (our state, is it booked?). Kept as a lookup so an
# unfamiliar status becomes "unknown" rather than silently reading as "not booked" — a
# false "nothing booked" sends someone to re-book a session they already have.
_STATUS: dict[str, tuple[str, bool]] = {
    "booked": ("booked", True),
    "available": ("available", False),
    "unavailable": ("unavailable", False),
}

# ── payment items ────────────────────────────────────────────────────────────
# The school raises charges here — a trip, swimming, a residential. These are
# server-rendered ASP.NET, with no JSON API behind them (unlike the clubs
# calendar), so this is the one place the adapter parses HTML. It anchors on the
# repeater's element ids rather than on layout classes, because the ids come from
# the server control's name and the Bootstrap grid around them does not.
ITEMS_PAGE = "/V3Payer4W3/Home/PaymentItems/PaymentItems.aspx"
_ITEM_BLOCK_RE = re.compile(r'<div id="body_body_rptPaymentItems_PaymentItem_\d+"')
_ITEM_NEW_RE = re.compile(r"rptPaymentItems_imgIsNew\d*_\d+")
_ITEM_DETAIL_RE = re.compile(r"ShowDetailView\((\d+),\s*(\d+)")
# "17 Sep 26" — the only date format these pages use.
_ITEM_DATE_FMT = "%d %b %y"


def _text(raw: str) -> str:
    """Tag-stripped, entity-decoded, whitespace-collapsed. The pages carry &pound; and
    non-breaking spaces around every money value, so decoding is not optional."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", raw or "")).replace(" ", " ").split())


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

        # No ParentPay password exists to ask for: the session is minted from Weduc's,
        # so this can always heal itself as long as the Weduc credentials are present.
        ok, detail = await self._recapture(ctx)
        if ok and await self._session_alive(ctx):
            return {"mode": "live", "state_path": str(state), "note": "session re-captured"}
        return {"mode": "fixtures",
                "warning": f"could not sign in to ParentPay ({detail}); used fixtures"}

    async def collect(self, ctx: SourceContext, session: dict[str, Any]) -> CollectResult:
        subject = ctx.subject or "luke"
        if session.get("mode") == "live":
            try:
                return await self._collect_live(ctx, subject)
            except Exception as e:
                # Never let a failed fetch masquerade as "nothing is booked".
                return CollectResult(
                    ok=False, live=False,
                    detail=(f"live fetch failed ({type(e).__name__}: {e}) — kept the "
                            f"previous data rather than reporting no bookings"))
        if ctx.live:
            return CollectResult(
                ok=False, live=False,
                detail=(session.get("warning")
                        or "no live ParentPay session; refusing to pass fixtures off as real"))
        return self._collect_fixtures(ctx, subject, warning=session.get("warning", ""))

    async def healthcheck(self, ctx: SourceContext) -> HealthResult:
        if ctx.live:
            if await self._session_alive(ctx):
                return HealthResult(ok=True, detail="live ParentPay session valid")
            return HealthResult(ok=False, detail="session expired — will re-mint from Weduc")
        p = ctx.fixtures_path()
        ok = bool(p and (p / "snapshot.json").exists())
        return HealthResult(ok=ok, detail="fixtures present" if ok else "snapshot.json missing")

    # ── session plumbing ─────────────────────────────────────────────────────
    def _abs(self, raw: Optional[str], default: str) -> Path:
        p = Path(raw or default)
        return p if p.is_absolute() else (_REPO_ROOT / p)

    def _state_path(self, ctx: SourceContext) -> Path:
        return self._abs(ctx.options.get("state_file"), "data/parentpay_state.json")

    def _weduc_state_path(self, ctx: SourceContext) -> Path:
        return self._abs(ctx.options.get("weduc_state_file"), "data/weduc_state.json")

    @staticmethod
    def _load_cookies(state_path: Path) -> list[dict[str, Any]]:
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        cookies = data.get("cookies") if isinstance(data, dict) else None
        if not isinstance(cookies, list):
            return []
        return [c for c in cookies if "parentpay" in (c.get("domain") or "")]

    def _client(self, ctx: SourceContext):
        import httpx
        jar = httpx.Cookies()
        for c in self._load_cookies(self._state_path(ctx)):
            jar.set(c["name"], c.get("value", ""),
                    domain=(c.get("domain") or ""), path=c.get("path", "/"))
        return httpx.AsyncClient(base_url=PP_BASE, cookies=jar, headers=_HEADERS,
                                 timeout=30.0, follow_redirects=True)

    @staticmethod
    async def _get_json(client, path: str) -> Any:
        """Fetch and parse, tolerating the sign-in shell. Never raises.

        A dead session answers 200 with HTML, so "did it parse as JSON" is the real
        liveness test — a status code is not.
        """
        try:
            r = await client.get(path)
            text = r.text.lstrip()
            if not text or text[0] not in "[{":
                return None
            return json.loads(text)
        except Exception:
            return None

    async def _member_id(self, client, club_id: str, consumer_id: str) -> Optional[int]:
        """The calendar endpoint keys on an internal member id, not the ConsumerId that
        appears in the page URL — they are different numbers for the same child."""
        body = await self._get_json(client, f"{API}/internal-details/{club_id}/{consumer_id}")
        if isinstance(body, dict):
            mid = body.get("internalMemberId")
            if isinstance(mid, int):
                return mid
        return None

    async def _session_alive(self, ctx: SourceContext) -> bool:
        if not self._load_cookies(self._state_path(ctx)):
            return False
        clubs = self._clubs(ctx)
        if not clubs:
            return False
        try:
            async with self._client(ctx) as client:
                return await self._member_id(
                    client, clubs[0]["id"], self._consumer_id(ctx)) is not None
        except Exception:
            return False

    async def _recapture(self, ctx: SourceContext) -> tuple[bool, str]:
        try:
            from .login import capture_session
        except Exception as e:
            return False, f"login helper unavailable: {type(e).__name__}"
        try:
            return await capture_session(
                state_file=self._state_path(ctx),
                weduc_state=self._weduc_state_path(ctx),
                entity_id=str(ctx.options.get("entity_id") or ""),
                child_id=str(ctx.options.get("child_id") or ""),
                weduc_username=ctx.secrets.get("weduc_username", ""),
                weduc_password=ctx.secrets.get("weduc_password", ""),
            )
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    # ── config ───────────────────────────────────────────────────────────────
    @staticmethod
    def _consumer_id(ctx: SourceContext) -> str:
        return str(ctx.options.get("consumer_id") or "")

    @staticmethod
    def _clubs(ctx: SourceContext) -> list[dict[str, str]]:
        """[{id, name, kind}] from config. `kind` lets a rule say "before school"."""
        out = []
        for c in (ctx.options.get("clubs") or []):
            if isinstance(c, dict) and c.get("id"):
                out.append({"id": str(c["id"]), "name": str(c.get("name") or "Club"),
                            "kind": str(c.get("kind") or "club")})
        return out

    @staticmethod
    def _tz(ctx: SourceContext):
        from zoneinfo import ZoneInfo
        try:
            return ZoneInfo(str(ctx.options.get("tz", "Europe/London")))
        except Exception:
            return ZoneInfo("UTC")

    # ── live collection ──────────────────────────────────────────────────────
    async def _collect_live(self, ctx: SourceContext, subject: str) -> CollectResult:
        clubs = self._clubs(ctx)
        if not clubs:
            return CollectResult(ok=False, live=False,
                                 detail="no clubs configured (sources.parentpay.options.clubs)")
        consumer = self._consumer_id(ctx)
        if not consumer:
            return CollectResult(ok=False, live=False, detail="no consumer_id configured")

        tz = self._tz(ctx)
        today = datetime.now(tz).date()
        horizon = today + timedelta(days=int(ctx.options.get("days_ahead", 21)))

        items: list[Item] = []
        club_states: list[dict[str, Any]] = []
        async with self._client(ctx) as client:
            for club in clubs:
                mid = await self._member_id(client, club["id"], consumer)
                if mid is None:
                    raise RuntimeError(
                        f"{club['name']}: no internal member id — session likely expired")
                cal = await self._get_json(
                    client, f"{API}/club-booking-calendar/{club['id']}/{mid}")
                if not isinstance(cal, dict) or "sessions" not in cal:
                    raise RuntimeError(f"{club['name']}: no calendar payload returned")

                choices = {c.get("id"): c for c in
                           ((cal.get("bookingChoiceSettings") or {}).get("bookingChoices") or [])
                           if isinstance(c, dict)}
                cutoff_cfg = cal.get("bookingCutOffSettings") or {}
                days = []
                for s in cal.get("sessions") or []:
                    d = self._session_date(s.get("date"))
                    if d is None or not (today <= d <= horizon):
                        continue
                    days.append(self._session_row(s, d, choices, cutoff_cfg, tz))

                for d in days:
                    items.append(Item(
                        source_id="parentpay", subject_ids=[subject],
                        external_id=f"parentpay:{club['id']}:{d['date']}",
                        kind="club_booking",
                        title=f"{club['name']} — {d['state']}",
                        body_text=self._day_detail(d),
                        occurred_at=datetime.combine(
                            date.fromisoformat(d["date"]), datetime.min.time()),
                        audience_tags=[club["kind"], d["state"]],
                        url=f"{PP_BASE}{CAL_PAGE}?ConsumerId={consumer}&ClubId={club['id']}",
                        raw={**d, "club_id": club["id"], "club_name": club["name"],
                             "club_kind": club["kind"]},
                    ))

                club_states.append({
                    "id": club["id"], "name": cal.get("clubName") or club["name"],
                    "kind": club["kind"],
                    "cutoff": cal.get("bookingCutOffInfo", ""),
                    "cutoff_settings": cutoff_cfg,
                    "balance": cal.get("balance"),
                    "can_book": bool(cal.get("canBookSessions")),
                    "url": f"{PP_BASE}{CAL_PAGE}?ConsumerId={consumer}&ClubId={club['id']}",
                    "days": days,
                })

            # Charges the school has raised (a trip, swimming). Separate from clubs and
            # tolerant of failure: a payment list that will not load must not lose the
            # booking data collected above.
            pay_items = await self._collect_payment_items(client)

        payments = self._payments_summary(pay_items, today, consumer)
        for it in pay_items:
            due = it.get("due") or ""
            items.append(Item(
                source_id="parentpay", subject_ids=[subject],
                external_id=f"parentpay:item:{it['item_id'] or it['name']}",
                kind="payment_item",
                title=it["name"],
                body_text=self._item_detail(it),
                occurred_at=(datetime.combine(date.fromisoformat(due), datetime.min.time())
                             if due else None),
                audience_tags=[it["kind"], it["status"]] + (["new"] if it["is_new"] else []),
                url=it.get("url", ""),
                raw=it,
            ))

        state = self._state_of_play(subject, club_states, today, tz)
        state["payments"] = payments
        state["data_quality"] = {"live": True, "collected_at": utcnow().isoformat(),
                                 "note": "real data from ParentPay"}
        signals = self._signals(subject, state, today)
        booked = sum(1 for c in club_states for d in c["days"] if d["booked"])
        nxt = state["next_unbooked"]
        detail = (f"live: {len(items)} club-days across {len(club_states)} club(s), "
                  f"{booked} booked; "
                  f"{len(payments['outstanding'])} payment(s) due "
                  f"(£{payments['total_due']:.2f}); next bookable gap: "
                  f"{(nxt['date'] + ' ' + nxt['club']) if nxt else 'none'}")
        return CollectResult(ok=True, live=True, items=items, signals=signals,
                             next_cursor=utcnow().isoformat(), detail=detail, state=state)

    # ── session normalisation ────────────────────────────────────────────────
    @staticmethod
    def _session_date(raw: Any) -> Optional[date]:
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
        except Exception:
            return None

    @classmethod
    def _session_row(cls, s: dict[str, Any], d: date, choices: dict[Any, dict],
                     cutoff_cfg: dict[str, Any], tz) -> dict[str, Any]:
        state, booked = _STATUS.get(str(s.get("status") or "").strip().lower(),
                                    ("unknown", False))
        choice = choices.get(s.get("bookingChoiceId")) or {}
        cap = s.get("capacity")
        used = s.get("bookingsAndReservationsCount")
        places = max(0, cap - used) if isinstance(cap, int) and isinstance(used, int) else None
        return {
            "date": str(d),
            "state": state,
            "booked": booked,
            "choice": str(choice.get("name") or ""),
            "price": choice.get("price"),
            "places_left": places,
            "capacity": cap,
            "reason": str(s.get("sessionUnavailableReason") or ""),
            "book_by": cls._cutoff_for(d, cutoff_cfg),
        }

    @staticmethod
    def _cutoff_for(d: date, cfg: dict[str, Any]) -> str:
        """When bookings for `d` close, as a local ISO timestamp.

        The two clubs at this school use different schemes, and the difference is the
        whole reason to compute this rather than repeat one sentence:
          * ``DayBeforeTimeBefore`` (breakfast club) — 09:00 the DAY BEFORE.
          * ``TheDayOfBefore`` (after-school club)   — 09:00 ON the day itself.
        So the same "not booked for Friday" is urgent on Thursday for one club and still
        fine on Friday morning for the other.

        An unrecognised scheme returns "" and the UI falls back to quoting the club's own
        sentence. Inventing a deadline would be worse than showing none.
        """
        kind = str(cfg.get("type") or "")
        try:
            hh, mm = str(cfg.get("timeCutOff") or "00:00").split(":")[:2]
            at = time(int(hh), int(mm))
        except Exception:
            return ""
        if kind == "DayBeforeTimeBefore":
            try:
                days = int(cfg.get("daysBeforeSession") or 0)
            except Exception:
                return ""
            return datetime.combine(d - timedelta(days=days), at).isoformat()
        if kind == "TheDayOfBefore":
            return datetime.combine(d, at).isoformat()
        return ""

    @staticmethod
    def _day_detail(d: dict[str, Any]) -> str:
        if d["booked"]:
            price = f" £{d['price']:.2f}" if isinstance(d["price"], (int, float)) else ""
            return f"Booked{(' — ' + d['choice']) if d['choice'] else ''}{price}"
        if d["state"] == "available":
            left = f" — {d['places_left']} places left" if d["places_left"] is not None else ""
            return f"Not booked{left}"
        if d["state"] == "unavailable":
            return f"Cannot book{(' — ' + d['reason']) if d['reason'] else ''}"
        return d["state"]

    @staticmethod
    def _item_detail(it: dict[str, Any]) -> str:
        if it["status"] == "due":
            amt = f"£{it['amount']:.2f}" if it["amount"] is not None else "amount unknown"
            return f"{amt} due{(' by ' + it['due']) if it['due'] else ''}"
        if it["status"] == "paid":
            return "Paid"
        amt = f"£{it['amount']:.2f}" if it["amount"] is not None else ""
        return f"Account balance {amt}".strip()

    # ── payment items (trips, swimming, residentials) ────────────────────────
    async def _collect_payment_items(self, client) -> list[dict[str, Any]]:
        """Charges the school has raised. Never raises — a payment list that fails to
        load must not take the club bookings down with it."""
        try:
            r = await client.get(ITEMS_PAGE, headers={"Accept": "text/html,*/*"})
            html = r.text
        except Exception:
            return []
        if "rptPaymentItems_PaymentItem_" not in html:
            return []
        return self._parse_payment_items(html)

    @classmethod
    def _parse_payment_items(cls, html: str) -> list[dict[str, Any]]:
        """One row per charge on the Payment items page.

        Each block flattens to a predictable little sequence, and the shape of it says
        what KIND of thing it is:

            Payment item: | <name> | Payment due: | <date> | <amount or "Paid"> | View
            Payment item: | <name> | Balance:     | <amount> [| <amount>]      | View

        A "Payment due" row is a one-off charge somebody has to settle; a "Balance" row
        is a running account (meals, the clubs) that funds itself and is not a task. Only
        the first kind can be outstanding, so only it can ever become an action.
        """
        starts = [m.start() for m in _ITEM_BLOCK_RE.finditer(html)]
        if not starts:
            return []
        bounds = starts + [len(html)]
        out: list[dict[str, Any]] = []
        for i in range(len(starts)):
            blk = html[bounds[i]:bounds[i + 1]]
            # flatten tags to separators so the label/value pairs line up
            flat = re.sub(r"<[^>]+>", "|", blk)
            segs = [_text(x) for x in flat.split("|")]
            segs = [x for x in segs if x]
            if "Payment item:" not in segs:
                continue
            name = segs[segs.index("Payment item:") + 1] if                 segs.index("Payment item:") + 1 < len(segs) else ""
            if not name:
                continue

            detail = _ITEM_DETAIL_RE.search(blk)
            row: dict[str, Any] = {
                "name": name,
                "item_id": detail.group(1) if detail else "",
                "consumer_id": detail.group(2) if detail else "",
                "is_new": bool(_ITEM_NEW_RE.search(blk)),
                "kind": "account",
                "due": "", "amount": None, "status": "", "outstanding": False,
            }
            if "Payment due:" in segs:
                j = segs.index("Payment due:")
                row["kind"] = "charge"
                row["due"] = cls._item_date(segs[j + 1] if j + 1 < len(segs) else "")
                status = segs[j + 2] if j + 2 < len(segs) else ""
                # "Paid" where the money would be is how a settled charge renders.
                if status.lower().startswith("paid"):
                    row["status"] = "paid"
                else:
                    row["status"] = "due"
                    row["amount"] = cls._money(status)
                    row["outstanding"] = True
            elif "Balance:" in segs:
                j = segs.index("Balance:")
                row["status"] = "account"
                row["amount"] = cls._money(segs[j + 1] if j + 1 < len(segs) else "")
            out.append(row)
        return out

    @staticmethod
    def _money(text: str) -> Optional[float]:
        m = re.search(r"-?[\d,]+\.\d{2}", (text or "").replace(" ", " "))
        if not m:
            return None
        try:
            return float(m.group(0).replace(",", ""))
        except ValueError:
            return None

    @staticmethod
    def _item_date(text: str) -> str:
        try:
            return datetime.strptime((text or "").strip(), _ITEM_DATE_FMT).date().isoformat()
        except ValueError:
            return ""

    @staticmethod
    def _payments_summary(items: list[dict[str, Any]], today: date,
                          consumer: str) -> dict[str, Any]:
        outstanding = [i for i in items if i["outstanding"]]
        outstanding.sort(key=lambda i: (i["due"] or "9999-12-31", i["name"]))
        for i in outstanding:
            i["overdue"] = bool(i["due"]) and i["due"] < str(today)
            i["url"] = (f"{PP_BASE}{ITEMS_PAGE}?pId={i['item_id']}"
                        f"&cId={i['consumer_id'] or consumer}") if i["item_id"] else ""
        return {
            "outstanding": outstanding,
            "total_due": round(sum(i["amount"] or 0 for i in outstanding), 2),
            "all": items,
            "derivation": (
                "A payment item is outstanding when the page shows a 'Payment due' date "
                "and a money amount rather than 'Paid'. Rows showing a 'Balance' are "
                "running accounts (school meals, the clubs) that top up rather than being "
                "settled once, so they are never actions. 'New!' is ParentPay's own flag "
                "for an item the school has just raised."),
        }

    # ── "state of play" for the hub ──────────────────────────────────────────
    @classmethod
    def _state_of_play(cls, subject: str, clubs: list[dict[str, Any]], today: date,
                       tz) -> dict[str, Any]:
        by_date: dict[str, dict[str, Any]] = {}
        for c in clubs:
            for d in c["days"]:
                row = by_date.setdefault(d["date"], {"date": d["date"], "clubs": {}})
                row["clubs"][c["name"]] = {
                    k: d[k] for k in
                    ("state", "booked", "choice", "price", "places_left", "reason", "book_by")
                } | {"kind": c["kind"]}

        # "Bookable but not booked" is the only actionable state. An unavailable day is
        # not a problem to solve — the deadline has gone or the club is not running.
        now = datetime.now(tz).replace(tzinfo=None)
        next_unbooked = None
        gaps: list[dict[str, Any]] = []
        for iso in sorted(by_date):
            for name, info in by_date[iso]["clubs"].items():
                if info["state"] != "available":
                    continue
                book_by = info.get("book_by") or ""
                still_open = True
                if book_by:
                    try:
                        still_open = datetime.fromisoformat(book_by) > now
                    except Exception:
                        still_open = True
                gaps.append({"date": iso, "club": name, "kind": info["kind"],
                             "book_by": book_by, "still_open": still_open})
                if next_unbooked is None and still_open:
                    next_unbooked = gaps[-1]
        return {
            "source": "parentpay", "subject": subject, "today": str(today),
            "clubs": [{k: v for k, v in c.items() if k != "days"} for c in clubs],
            "days": [by_date[k] for k in sorted(by_date)],
            "unbooked": gaps,
            "next_unbooked": next_unbooked,
            "derivation": (
                "'available' means ParentPay is still accepting a booking for that day "
                "and none exists — the only state worth acting on. 'unavailable' means "
                "the cut-off has passed or the club is not running; 'booked' includes "
                "sessions already taken. `book_by` is computed from the club's own "
                "bookingCutOffSettings (typically 09:00 the DAY BEFORE), so a nudge on "
                "the morning of the session is already too late."),
        }

    @staticmethod
    def _signals(subject: str, state: dict[str, Any], today: date) -> list[Signal]:
        tomorrow = str(today + timedelta(days=1))
        row = next((r for r in state.get("days", []) if r["date"] == tomorrow), None)
        clubs = (row or {}).get("clubs", {})
        booked = any(i["booked"] for i in clubs.values())
        unbooked = any(i["state"] == "available" for i in clubs.values())
        pay = state.get("payments") or {}
        return [
            Signal(key=f"parentpay.{subject}.club_booked_tomorrow", value=bool(booked),
                   type=SignalType.bool, source="parentpay", subject=subject,
                   meta={"date": tomorrow,
                         "clubs": {n: i["state"] for n, i in clubs.items()}}),
            Signal(key=f"parentpay.{subject}.club_unbooked_tomorrow", value=bool(unbooked),
                   type=SignalType.bool, source="parentpay", subject=subject,
                   meta={"date": tomorrow, "next_unbooked": state.get("next_unbooked")}),
            Signal(key=f"parentpay.{subject}.payments_outstanding",
                   value=len(pay.get("outstanding") or []),
                   type=SignalType.number, source="parentpay", subject=subject,
                   meta={"total_due": pay.get("total_due", 0),
                         "items": [{"name": i["name"], "amount": i["amount"],
                                    "due": i["due"], "new": i["is_new"]}
                                   for i in (pay.get("outstanding") or [])]}),
        ]

    # ── fixtures (offline) ───────────────────────────────────────────────────
    def _collect_fixtures(self, ctx: SourceContext, subject: str,
                          warning: str = "") -> CollectResult:
        data: dict[str, Any] = {}
        p = ctx.fixtures_path()
        if p and (p / "snapshot.json").exists():
            data = json.loads((p / "snapshot.json").read_text(encoding="utf-8"))

        tz = self._tz(ctx)
        today = datetime.now(tz).date()
        items: list[Item] = []
        club_states: list[dict[str, Any]] = []
        # Fixture days are offsets so the sample never goes stale.
        for club in data.get("clubs", []):
            cutoff_cfg = club.get("cutoff_settings") or {
                "type": "DayBeforeTimeBefore", "daysBeforeSession": 1, "timeCutOff": "09:00"}
            days = []
            for d in club.get("days", []):
                dt = today + timedelta(days=int(d.get("offset", 0)))
                state = d.get("state", "available")
                row = {"date": str(dt), "state": state, "booked": state == "booked",
                       "choice": d.get("choice", ""), "price": d.get("price"),
                       "places_left": d.get("places_left"), "capacity": d.get("capacity"),
                       "reason": d.get("reason", ""),
                       "book_by": self._cutoff_for(dt, cutoff_cfg)}
                days.append(row)
                items.append(Item(
                    source_id="parentpay", subject_ids=[subject],
                    external_id=f"parentpay:{club['id']}:{dt}",
                    kind="club_booking",
                    title=f"{club['name']} — {row['state']}",
                    body_text=self._day_detail(row),
                    occurred_at=datetime.combine(dt, datetime.min.time()),
                    audience_tags=[club.get("kind", "club"), row["state"]],
                    raw={**row, "club_id": club["id"], "club_name": club["name"]},
                ))
            club_states.append({
                "id": str(club["id"]), "name": club["name"],
                "kind": club.get("kind", "club"), "cutoff": club.get("cutoff", ""),
                "cutoff_settings": cutoff_cfg, "balance": club.get("balance"),
                "can_book": True, "url": "", "days": days,
            })

        # Fixture due dates are offsets too, so the sample charge never drifts into
        # looking overdue just because the sample got old.
        pay_items = []
        for it in data.get("payment_items", []):
            off = it.get("due_offset")
            row = {k: v for k, v in it.items() if k != "due_offset"}
            row["due"] = str(today + timedelta(days=int(off))) if off is not None else ""
            pay_items.append(row)
        for it in pay_items:
            items.append(Item(
                source_id="parentpay", subject_ids=[subject],
                external_id=f"parentpay:item:{it['item_id'] or it['name']}",
                kind="payment_item", title=it["name"], body_text=self._item_detail(it),
                occurred_at=(datetime.combine(date.fromisoformat(it["due"]),
                                              datetime.min.time()) if it["due"] else None),
                audience_tags=[it["kind"], it["status"]] + (["new"] if it["is_new"] else []),
                raw=it,
            ))

        state = self._state_of_play(subject, club_states, today, tz)
        state["payments"] = self._payments_summary(
            pay_items, today, str(ctx.options.get("consumer_id") or ""))
        state["data_quality"] = {
            "live": False, "collected_at": utcnow().isoformat(),
            "note": "SAMPLE DATA — offline fixtures, not real ParentPay bookings",
        }
        detail = f"fixtures: {len(items)} club-days across {len(club_states)} club(s)"
        if warning:
            detail += f" — {warning}"
        return CollectResult(ok=True, live=False, items=items,
                             signals=self._signals(subject, state, today),
                             next_cursor=utcnow().isoformat(), detail=detail, state=state)
