"""Daily reminders — "what does Luke need tomorrow?", answered from the school portal.

Why this exists: the Weduc adapter now brings back genuinely good information — the class
calendar, the head's messages, outstanding forms, term boundaries and the newsletter
digest — but it lands as 140 undifferentiated Items on the Sources tab. The thing a
parent actually needs is the eight-in-the-morning question: is there school, what does he
take, and is there anything I was supposed to have done. This module answers exactly that.

Two layers, and the split matters:

* A **deterministic spine** that always works — no API key, no network. Term/closure days
  come from the adapter's school context, dated events from the calendar Items, forms from
  the stored state. If the LLM is off or fails, the page is still correct, just thinner.

* An **LLM pass** over messages and posts, which is where the un-mechanical value hides:
  "please can each child bring in a plastic carrier bag tomorrow" is a Thursday reminder
  only if you know the message was sent on the Wednesday. Resolving that needs language,
  so per the adapter contract (docs/sourceadapterCONTRACT.md §2 — adapters stay
  mechanical, the hub interprets) it happens here rather than in the adapter.

The LLM proposes and this module verifies, the same way :mod:`warden.documents` does with
newsletter claims: a reminder survives only if it cites an item we actually sent, quotes
words that actually appear in it, and lands on a date that is not already past. Rejected
ones are kept under ``unverified`` rather than binned silently. An invented "bring £5 on
Friday" is worse than no reminder at all — the parent stops trusting the page.

Results are cached in the kv store under a hash of the inputs, so the reading is paid for
once per genuine change rather than once per page view.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger("warden.reminders")

# How far the day-by-day view runs, and how far back a message can still be talking
# about something in that window.
DAYS_AHEAD = 14
MESSAGE_LOOKBACK_DAYS = 30
MAX_LLM_ITEMS = 25
MAX_BODY_CHARS = 700
# After this long, a newsletter's undated asks are assumed dealt with or expired.
NEWSLETTER_FRESH_DAYS = 21

# Kinds shown on a day, in the order they matter at 8am. Anything else is coerced to
# "note" — an unknown kind must not become an unstyled chip in the UI. Only the subset
# in LLM_KINDS is offered to the model; `club` and `lunch` are derived from booking data,
# never read out of prose, so letting the model emit them would blur a hard fact with a
# guess.
KINDS = ("club", "kit", "bring", "wear", "money", "trip", "food", "lunch", "form",
         "event", "note")
LLM_KINDS = ("kit", "bring", "wear", "money", "trip", "food", "form", "event", "note")

_SYSTEM = (
    "You turn a UK primary school's portal messages into a parent's day-by-day reminder "
    "list. The parent wants the 8am questions answered: what must the child TAKE IN, "
    "WEAR, or be given MONEY for, and what must the parent DO — the things that are easy "
    "to miss.\n"
    "\n"
    "Rules, in order of importance:\n"
    "1. Report ONLY what a message actually says. Never invent an item, a date, an amount "
    "or a name. Returning fewer reminders is a good answer; inventing one is not.\n"
    "2. Resolve every relative date ('tomorrow', 'on Friday', 'next week') against the "
    "SENT date of the message it appears in — each message below is labelled with its own "
    "sent date. Output an absolute date as YYYY-MM-DD.\n"
    "3. Leave out anything dated before today, anything already superseded, and messages "
    "that ask nothing of anyone (building updates, thank-yous, results).\n"
    "   Judge each ASK, not the message's overall tone. A cheerful newsy message that "
    "also carries one dated thing to do — a deadline to beat, a closing date, something "
    "to sign up for — still yields that one reminder. Extract it and drop the rest of "
    "the message.\n"
    "4. `quote` must be a SHORT run of words copied verbatim from that message's text. It "
    "is checked against the original; a paraphrase is discarded.\n"
    "5. If the message is addressed to one class, copy that class name into `for_class` "
    "exactly as written. Otherwise leave it empty.\n"
    "6. `title` is an imperative of five words or fewer ('Bring a plastic bag'). `detail` "
    "is one short sentence of specifics.\n"
    "\n"
    f"`kind` must be one of: {', '.join(LLM_KINDS)}.\n"
    "Use `certainty` \"stated\" when the message gives the day outright, \"inferred\" when "
    "you worked it out from wording like 'tomorrow'.\n"
    "\n"
    "Reply with ONLY a JSON object, at most 12 reminders, shortest fields that still make "
    "sense:\n"
    '{"reminders": [{"source_id": "<the id given with the message>", '
    '"date": "YYYY-MM-DD", "title": "<imperative>", "detail": "<one sentence>", '
    '"kind": "<kind>", "quote": "<verbatim words>", "for_class": "<class or empty>", '
    '"certainty": "stated|inferred"}]}'
)


class ReminderBuilder:
    """Builds the Reminders page payload from stored Weduc data."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx

    # ── public entrypoint ────────────────────────────────────────────────────
    async def build(self, force: bool = False) -> dict[str, Any]:
        """The whole page, deterministic parts always fresh, LLM part cached."""
        tz = self._tz()
        now = datetime.now(tz)
        today = now.date()

        state = self._state()
        items = self._items()
        subject = self._subject()
        child_class = self._child_class(state, today)
        classes = self._known_classes(items, child_class)

        school = state.get("school") or {}
        days = self._day_spine(today, school)
        by_date: dict[str, list[dict]] = {d["date"]: d["reminders"] for d in days}

        # 1. dated calendar events, filtered to this child's class + whole-school
        for rem in self._event_reminders(items, today, tz, child_class, classes):
            by_date.setdefault(rem["date"], []).append(rem)

        # 2. term boundaries / closures worth calling out on the day itself
        for rem in self._school_day_reminders(days, school):
            by_date[rem["date"]].append(rem)

        # 3a. school lunches — the choice, and the days with nothing ordered
        for rem in self._meal_reminders(state, today):
            by_date.setdefault(rem["date"], []).append(rem)

        # 3b. breakfast / after-school club bookings (ParentPay)
        club_state = self._club_state()
        for rem in self._club_reminders(club_state, today, now):
            by_date.setdefault(rem["date"], []).append(rem)

        # 3. the newsletter's own dated notes
        docs = state.get("documents") or []
        undated: list[dict] = []
        for rem in self._newsletter_reminders(docs, today):
            if rem["date"]:
                by_date.setdefault(rem["date"], []).append(rem)
            else:
                undated.append(rem)

        # 4. the LLM pass over messages/posts — additive, and never fatal
        llm_meta: dict[str, Any] = {"used": False, "reason": "", "kept": 0, "dropped": 0}
        unverified: list[dict] = []
        try:
            found, llm_meta, unverified = await self._llm_reminders(
                items, today, tz, subject, child_class, classes, force=force)
            for rem in found:
                by_date.setdefault(rem["date"], []).append(rem)
        except Exception as e:                      # a reading failure must not blank the page
            log.warning("reminder reading failed: %s: %s", type(e).__name__, e)
            llm_meta = {"used": False, "reason": f"{type(e).__name__}: {e}",
                        "kept": 0, "dropped": 0}

        # A nudge to order meals is pointless once the meals are ordered — drop it after
        # the LLM pass, when both the prose and the booking data are in hand.
        llm_meta["suppressed_answered"] = self._drop_answered_meal_asks(by_date, state)

        # Days the spine didn't cover (an LLM or newsletter date past the window) still
        # need a row, or the reminder simply vanishes.
        for iso in sorted(by_date):
            if not any(d["date"] == iso for d in days):
                days.append(self._bare_day(iso, today, school))
        days.sort(key=lambda d: d["date"])
        for d in days:
            # Anything actionable first, then by kind. A missing lunch booking outranks
            # a note about the newsletter no matter what kind order says.
            d["reminders"] = self._dedupe(sorted(
                by_date.get(d["date"], []),
                key=lambda r: (not r.get("alert"),
                               KINDS.index(r["kind"]) if r["kind"] in KINDS else 99)))

        forms = self._forms(state, items, now)
        # The only thing that raises an alert now is a school day with NO lunch ordered —
        # a real gap with nothing else to catch it. Unbooked club sessions deliberately
        # don't: see _club_reminders.
        alerts = sorted((r for d in days for r in d["reminders"] if r.get("alert")),
                        key=lambda r: r["date"])
        return {
            "generated_at": now.isoformat(),
            "today": str(today),
            "tz": str(tz),
            "subject": subject | {"class": child_class},
            "school": self._school_summary(days, school),
            "days": days,
            "forms": forms,
            "payments": self._payments(club_state, today),
            "alerts": alerts,
            "clubs": self._clubs_summary(club_state, now),
            "actions": undated,
            "newsletter": self._newsletter_summary(docs, classes, today),
            "data": self._data_quality(state, now),
            "llm": llm_meta,
            "unverified": unverified,
        }

    # ── inputs ───────────────────────────────────────────────────────────────
    def _tz(self):
        try:
            return ZoneInfo(self.ctx.settings.tz)
        except Exception:
            return timezone.utc

    def _state(self) -> dict[str, Any]:
        raw = self.ctx.db.get_kv("state:weduc")
        if not raw:
            return {}
        try:
            return json.loads(raw) or {}
        except Exception:
            return {}

    def _items(self) -> list:
        # 141 Weduc items today; pulling the lot is cheaper than a second round trip and
        # lets us sort by occurrence rather than by when we happened to fetch.
        return self.ctx.db.recent_items("weduc", 800)

    def _subject(self) -> dict[str, Any]:
        src = self.ctx.config.source("weduc")
        sid = (src.subject if src else None) or "luke"
        s = self.ctx.config.subject(sid)
        return {"id": sid, "name": s.name if s else sid}

    def _child_class(self, state: dict, today: date) -> str:
        """The portal's own class name wins; config contexts are the offline fallback.

        The portal rolls a child into their new class over the summer, and it is the
        thing the calendar and post tags are actually written against — so a config
        `effective_from` that has not landed yet must not override what the school says.
        """
        live = str(state.get("class") or "").strip()
        if live:
            return live
        s = self.ctx.config.subject(self._subject()["id"])
        for c in (s.contexts if s else []):
            frm, to = c.effective_from, c.effective_to
            if (not frm or frm <= str(today)) and (not to or str(today) < to):
                return c.label
        return ""

    # ── class relevance ──────────────────────────────────────────────────────
    @staticmethod
    def _class_tokens(label: str) -> set[str]:
        """"Swordfish Class" → {"swordfish class", "swordfish"}.

        The calendar drops the word "Class" as often as it keeps it ("Swordfish
        Swimming/PE" vs "Jellyfish Class Swimming/PE"), so match on the bare name too.
        """
        lab = " ".join(str(label).lower().split())
        if not lab:
            return set()
        return {lab, re.sub(r"\s*class$", "", lab).strip()} - {""}

    def _known_classes(self, items: list, child_class: str) -> dict[str, set[str]]:
        """Every class name this school uses, split into "his" and "someone else's".

        Read from the audience tags the portal itself puts on posts, so a school that
        renames its classes needs no code change. Config contexts are folded in because
        last year's class still appears on older items.
        """
        labels: set[str] = set()
        for it in items:
            for tag in (it.audience_tags or []):
                if re.search(r"\bclass\b", str(tag), re.I):
                    labels.add(str(tag).strip())
        for s in self.ctx.config.subjects:
            for c in s.contexts:
                if c.label:
                    labels.add(c.label)
        if child_class:
            labels.add(child_class)

        mine = self._class_tokens(child_class)
        others: set[str] = set()
        for lab in labels:
            toks = self._class_tokens(lab)
            if toks & mine:
                continue
            others |= toks
        return {"mine": mine, "others": others}

    @staticmethod
    def _mentions(text: str, tokens: set[str]) -> bool:
        low = (text or "").lower()
        return any(re.search(rf"\b{re.escape(t)}\b", low) for t in tokens if t)

    def _relevance(self, text: str, classes: dict[str, set[str]]) -> str:
        """mine | other | school — "other" is what keeps another class's swimming slot
        off the page. A title naming his class AND another still counts as his."""
        if self._mentions(text, classes["mine"]):
            return "mine"
        if self._mentions(text, classes["others"]):
            return "other"
        return "school"

    # ── the day spine ────────────────────────────────────────────────────────
    def _day_spine(self, today: date, school: dict) -> list[dict]:
        """One row per day from today, school status looked up by real date.

        `upcoming_days` was computed when the source last ran, so its first entry is the
        collection's "today", not necessarily ours. Looking each date up by key rather
        than by position is what stops a stale overnight snapshot shifting every day's
        status by one.
        """
        known = {d.get("date"): d for d in (school.get("upcoming_days") or [])}
        out = []
        for n in range(DAYS_AHEAD):
            d = today + timedelta(days=n)
            out.append(self._day_row(d, today, known.get(str(d))))
        return out

    def _bare_day(self, iso: str, today: date, school: dict) -> dict:
        known = {d.get("date"): d for d in (school.get("upcoming_days") or [])}
        try:
            d = date.fromisoformat(iso)
        except ValueError:
            d = today
        return self._day_row(d, today, known.get(iso))

    def _day_row(self, d: date, today: date, known: Optional[dict]) -> dict:
        delta = (d - today).days
        # "Thu 3 Sep" assembled by hand — the no-pad day directive is %-d on POSIX and
        # %#d on Windows, and this runs on both (native here, Linux in the container).
        label = ("Today" if delta == 0 else "Tomorrow" if delta == 1
                 else f"{d.strftime('%a')} {d.day} {d.strftime('%b')}")
        return {
            "date": str(d),
            "weekday": d.strftime("%A"),
            "label": label,
            "delta": delta,
            "is_school_day": (known or {}).get("is_school_day"),
            "why": (known or {}).get("why", "beyond the collected school calendar"),
            "reminders": [],
        }

    def _school_summary(self, days: list[dict], school: dict) -> dict:
        today_row = days[0] if days else {}
        tomorrow = next((d for d in days if d["delta"] == 1), {})
        return {
            "today": today_row,
            "tomorrow": tomorrow,
            "term": school.get("current_term"),
            "in_holidays": school.get("in_holidays"),
            "next_school_day": school.get("next_school_day"),
            "next_term_start": school.get("next_term_start"),
            "confidence": school.get("confidence", ""),
        }

    def _school_day_reminders(self, days: list[dict], school: dict) -> list[dict]:
        """Call out the transitions worth waking up for.

        Only breaks that are NOT just a weekend earn a "back to school": every Monday
        follows a Saturday, so firing on that put a reminder on the page every week that
        told the parent nothing and taught them to skim past the ones that matter.
        """
        out: list[dict] = []
        prev: Optional[bool] = None
        break_reasons: list[str] = []
        for d in days:
            cur = d["is_school_day"]
            if cur is False:
                break_reasons.append(d["why"])
            if cur is True and prev is False:
                real = [w for w in break_reasons if w != "weekend"]
                if real:
                    out.append(self._rem(
                        d["date"], "Back to school",
                        f"First day back after the {real[-1]}.", "event",
                        source={"kind": "school_calendar"}, certainty="stated"))
                break_reasons = []
            if cur is False and prev is True and d["why"] != "weekend":
                out.append(self._rem(d["date"], f"No school — {d['why']}",
                                     "School is closed this day.", "event",
                                     source={"kind": "school_calendar"}, certainty="stated"))
            prev = cur
        return out

    # ── calendar events ──────────────────────────────────────────────────────
    def _event_reminders(self, items: list, today: date, tz, child_class: str,
                         classes: dict[str, set[str]]) -> list[dict]:
        horizon = today + timedelta(days=DAYS_AHEAD - 1)
        out = []
        for it in items:
            if it.kind != "calendar_event" or not it.occurred_at:
                continue
            when = self._local(it.occurred_at, tz)
            d = when.date()
            if not (today <= d <= horizon):
                continue
            title = it.title or "(untitled event)"
            rel = self._relevance(title, classes)
            if rel == "other":
                continue                     # another class's slot is not his reminder
            all_day = bool((it.raw or {}).get("allDay"))
            # An all-day flag is the only trustworthy "no time" marker: some feed rows
            # carry a start of 00:00 and others a real clock time for the same event.
            at = "" if all_day else when.strftime("%H:%M")
            out.append(self._rem(
                str(d), title,
                self._event_detail(title, at, rel, child_class),
                self._event_kind(title),
                source={"kind": "calendar_event", "id": it.external_id, "title": title},
                at=at, scope=rel, certainty="stated"))
        return out

    @staticmethod
    def _event_detail(title: str, at: str, rel: str, child_class: str) -> str:
        who = f"{child_class}." if rel == "mine" and child_class else "Whole school."
        return f"{who}{(' At ' + at + '.') if at else ' All day.'}"

    # Titles that imply the child has to take or wear something. Deliberately narrow:
    # a wrong kind only mislabels a chip, but a missed one loses the visual cue.
    _KIT_WORDS = ("swimming", "pe", "forest school", "sports day", "cricket", "bell",
                  "bellboat", "boat")
    _WEAR_WORDS = ("dress up", "tag day", "non-uniform", "own clothes", "wear it",
                   "break the rules", "christmas jumper", "costume")
    _TRIP_WORDS = ("trip", "visit", "jamboree", "theatre", "church", "panto", "farm")
    _FOOD_WORDS = ("picnic", "lunch", "bbq", "dinner", "fayre", "fair", "pizza")

    @classmethod
    def _event_kind(cls, title: str) -> str:
        t = (title or "").lower()
        if any(w in t for w in cls._WEAR_WORDS):
            return "wear"
        if any(w in t for w in cls._TRIP_WORDS):
            return "trip"
        if any(w in t for w in cls._KIT_WORDS):
            return "kit"
        if any(w in t for w in cls._FOOD_WORDS):
            return "food"
        return "event"

    # ── lunches (Weduc) ──────────────────────────────────────────────────────
    def _meal_reminders(self, state: dict, today: date) -> list[dict]:
        """What he's having, and — louder — the school days with nothing ordered.

        Two different things share a row here on purpose. "Fish fingers on Friday" is a
        nicety; "nothing booked on Friday" is the reason to open the page at all, so it
        is a separate reminder with its own urgency rather than a quiet absence.
        """
        meals = state.get("meals") or {}
        out: list[dict] = []
        for m in (meals.get("days") or []):
            iso = m.get("date", "")
            if not iso or iso < str(today) or not m.get("booked"):
                continue
            choice = str(m.get("choice") or "").strip()
            if not choice:
                continue
            out.append(self._rem(
                iso, choice, f"School lunch{(' · ' + m['menu']) if m.get('menu') else ''}",
                "lunch", source={"kind": "meals"}, certainty="stated",
                badge={"text": "Booked", "tone": "ok"}))

        for gap in (meals.get("unbooked_school_days") or []):
            iso = gap.get("date", "")
            if not iso or iso < str(today):
                continue
            out.append(self._rem(
                iso, "School lunch", "Nothing ordered for this school day.",
                "lunch", source={"kind": "meals"}, certainty="stated",
                alert=True, badge={"text": "Order lunch", "tone": "danger"}))
        return out

    # Prose that is asking you to order school meals. Used only to drop a nudge the
    # booking data has already answered — never to create one.
    _MEAL_ASK = re.compile(
        r"\b(order|book|choose|select)\b.{0,40}\b(dinner|dinners|lunch|lunches|meal|meals)\b"
        r"|\b(dinner|dinners|lunch|lunches|meal|meals)\b.{0,40}\b(order|book|choose|select)",
        re.I)

    def _drop_answered_meal_asks(self, by_date: dict[str, list[dict]],
                                 state: dict) -> int:
        """Remove "order his school dinners" nudges when every school day is already booked.

        The messages tell you to order meals every term; the booking data tells you
        whether you actually need to. When nothing is outstanding the nudge is not just
        redundant, it is wrong — it sends you to re-do a job that is done. So the hard
        fact wins over the prose, and the nudge comes back the moment a real gap appears.
        """
        meals = state.get("meals") or {}
        if not meals or (meals.get("unbooked_school_days") or []):
            return 0                      # there IS something to order — keep the nudge
        dropped = 0
        for iso, rems in by_date.items():
            keep = []
            for r in rems:
                from_llm = (r.get("source") or {}).get("kind") == "message"
                if from_llm and self._MEAL_ASK.search(f"{r['title']} {r['detail']}"):
                    dropped += 1
                    continue
                keep.append(r)
            by_date[iso] = keep
        return dropped

    # ── clubs (ParentPay) ────────────────────────────────────────────────────
    def _club_state(self) -> dict[str, Any]:
        raw = self.ctx.db.get_kv("state:parentpay")
        if not raw:
            return {}
        try:
            return json.loads(raw) or {}
        except Exception:
            return {}

    def _club_reminders(self, club_state: dict, today: date,
                        now: datetime) -> list[dict]:
        """Sessions he IS booked into, as a green pill on the day. Nothing else.

        Deliberately one-sided. An unbooked session is not a problem to be told about:
        clubs are booked a week or two at a time, so most future days are legitimately
        empty, and listing them turned the page into a standing to-do list of things
        that were fine. The question this page answers is "is he in?", and the honest
        answer for an empty day is the absence of a row.

        The price is left out too — it is on the ParentPay page, and repeating it on
        every row every day is noise rather than reassurance.
        """
        out: list[dict] = []
        for row in (club_state.get("days") or []):
            iso = row.get("date", "")
            if not iso or iso < str(today):
                continue
            for name, info in (row.get("clubs") or {}).items():
                if info.get("state") != "booked":
                    continue
                out.append(self._rem(
                    iso, name, "", "club",
                    source={"kind": "parentpay", "title": name}, certainty="stated",
                    badge={"text": "Booked", "tone": "ok"}))
        return out

    # ── forms ────────────────────────────────────────────────────────────────
    def _forms(self, state: dict, items: list, now: datetime) -> list[dict]:
        """Outstanding forms, with a due date where the portal gave us one.

        The count comes from the adapter's state (its "Available Forms" list IS the
        outstanding set); due dates only exist on Items, so the two are joined by title.
        """
        titles = ((state.get("forms_outstanding") or {}).get("titles")) or []
        due_by_title, url_by_title = {}, {}
        for it in items:
            if it.kind != "form":
                continue
            t = it.title.strip()
            # Several rows can share a title (a re-issued form, or the offline fixture
            # sitting beside the live one). Any real due date beats none.
            if it.due_at and not due_by_title.get(t):
                due_by_title[t] = it.due_at
            if it.url and not url_by_title.get(t):
                url_by_title[t] = it.url
        out = []
        for t in titles:
            t = str(t).strip()
            due = due_by_title.get(t)
            out.append({
                "title": t,
                "due_at": due.isoformat() if due else None,
                "overdue": bool(due and due < now),
                "url": url_by_title.get(t),
            })
        out.sort(key=lambda f: (not f["overdue"], f["due_at"] or "9999"))
        return out

    # ── newsletter digest ────────────────────────────────────────────────────
    def _newsletter_reminders(self, docs: list, today: date) -> list[dict]:
        """The reader's `actions_for_parents` / `key_dates`, dated where they can be.

        An undated action ("sign up at the library") has no natural expiry, so it would
        sit on the page for ever — the July newsletter was still asking for the summer
        reading challenge in September. Actions therefore expire with the newsletter that
        carried them; a dated one still has to be in the future on its own merits.
        """
        out = []
        for doc in docs:
            name = (doc.get("_meta") or {}).get("document", "the newsletter")
            issued = self._loose_date(name, today)
            stale = bool(issued and (today - issued).days > NEWSLETTER_FRESH_DAYS)
            for a in (doc.get("actions_for_parents") or []):
                what = str(a.get("what") or "").strip()
                if not what:
                    continue
                when = self._loose_date(str(a.get("due") or ""), today)
                if when and when < today:
                    continue
                if stale and not when:
                    continue
                out.append(self._rem(
                    str(when) if when else "", self._trim(what, 100),
                    (str(a.get("due") or "").strip() or f"From {name}."), "note",
                    source={"kind": "newsletter", "title": name}, certainty="stated"))
            for k in (doc.get("key_dates") or []):
                when = self._loose_date(str(k.get("date") or ""), today)
                if not when or when < today:
                    continue
                out.append(self._rem(
                    str(when), self._trim(str(k.get("what") or ""), 100),
                    f"Noted in {name}.", "event",
                    source={"kind": "newsletter", "title": name}, certainty="stated"))
        return out

    def _newsletter_summary(self, docs: list, classes: dict[str, set[str]],
                            today: date) -> dict:
        """What the latest newsletter said about him and his class.

        `about_class` is filtered to his own class — the reader records every class it
        finds, and another class's merit list is not news about him. `issued`/`stale`
        ride along because "latest" and "recent" are not the same thing: over a holiday
        the newest newsletter can be six weeks old, and the page must not present that
        as this week's news.
        """
        if not docs:
            return {}
        doc = docs[0]
        name = (doc.get("_meta") or {}).get("document", "")
        issued = self._loose_date(name, today)
        mine = [c for c in (doc.get("about_class") or [])
                if self._relevance(str(c.get("class") or ""), classes) != "other"]
        return {
            "document": name,
            "issued": str(issued) if issued else "",
            "age_days": (today - issued).days if issued else None,
            "stale": bool(issued and (today - issued).days > NEWSLETTER_FRESH_DAYS),
            "summary": doc.get("summary", ""),
            "child_mentioned": bool(doc.get("child_mentioned")),
            "about_child": doc.get("about_child") or [],
            "about_class": mine,
        }

    def _payments(self, club_state: dict, today: date) -> list[dict]:
        """Charges from ParentPay that still need paying, soonest deadline first.

        Only genuine charges reach here — the adapter has already set aside the running
        balances for meals and clubs, which top themselves up and are never a task. So
        every row on this card is something a person has to go and do, which is what
        earns it a place beside the outstanding forms.
        """
        pay = (club_state or {}).get("payments") or {}
        out = []
        for i in (pay.get("outstanding") or []):
            out.append({
                "title": str(i.get("name") or "").strip(),
                "amount": i.get("amount"),
                "due": i.get("due") or "",
                "overdue": bool(i.get("due")) and str(i["due"]) < str(today),
                "is_new": bool(i.get("is_new")),
                "url": i.get("url") or "",
            })
        out.sort(key=lambda x: (not x["overdue"], x["due"] or "9999-12-31"))
        return out

    def _clubs_summary(self, club_state: dict, now: datetime) -> dict[str, Any]:
        """Club-level facts for the page header: cut-off wording, balance, freshness."""
        if not club_state:
            return {}
        dq = club_state.get("data_quality") or {}
        return {
            "clubs": [{"name": c.get("name"), "kind": c.get("kind"),
                       "cutoff": c.get("cutoff", ""), "balance": c.get("balance"),
                       "url": c.get("url", "")}
                      for c in (club_state.get("clubs") or [])],
            "next_unbooked": club_state.get("next_unbooked"),
            "live": bool(dq.get("live")),
            "collected_at": dq.get("collected_at", ""),
            "note": dq.get("note", ""),
        }

    @staticmethod
    def _trim(s: str, n: int) -> str:
        s = " ".join(str(s).split())
        return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"

    # ── the LLM pass ─────────────────────────────────────────────────────────
    async def _llm_reminders(self, items: list, today: date, tz, subject: dict,
                             child_class: str, classes: dict[str, set[str]],
                             force: bool = False,
                             ) -> tuple[list[dict], dict, list[dict]]:
        compiler = self.ctx.compiler
        if compiler is None or not compiler.has_llm():
            return [], {"used": False, "reason": "no LLM backend configured",
                        "kept": 0, "dropped": 0}, []

        pool = self._llm_pool(items, today, tz)
        if not pool:
            return [], {"used": False, "reason": "no recent messages to read",
                        "kept": 0, "dropped": 0}, []

        key = self._cache_key(pool, child_class)
        cached = None if force else self.ctx.db.get_kv(key)
        if cached:
            try:
                data = json.loads(cached)
                kept, dropped, unver = self._verify(data.get("reminders") or [], pool,
                                                    today, classes)
                return kept, {"used": True, "cached": True, "reason": "",
                              "kept": len(kept), "dropped": dropped,
                              "model": data.get("_model", "")}, unver
            except Exception:
                pass                                   # a bad cache entry just re-reads

        user = self._llm_user_message(pool, today, subject, child_class)
        model = self.ctx.settings.eval_model or None
        data = await compiler.complete_json(_SYSTEM, user, model=model)
        data["_model"] = model or self.ctx.settings.llm_model
        self.ctx.db.set_kv(key, json.dumps(data, default=str))

        kept, dropped, unver = self._verify(data.get("reminders") or [], pool, today, classes)
        log.info("read %d message(s) → %d reminder(s) kept, %d rejected",
                 len(pool), len(kept), dropped)
        await self.ctx.audit("weduc", "reminders_read", str(today),
                             f"read {len(pool)} messages; {len(kept)} reminders kept, "
                             f"{dropped} rejected as unverified")
        return kept, {"used": True, "cached": False, "reason": "",
                      "kept": len(kept), "dropped": dropped,
                      "model": data["_model"]}, unver

    def _llm_pool(self, items: list, today: date, tz) -> list[dict]:
        """Recent messages and posts, newest first — where the "bring X" asks live."""
        cutoff = today - timedelta(days=MESSAGE_LOOKBACK_DAYS)
        pool = []
        for it in items:
            if it.kind not in ("message", "post") or not it.occurred_at:
                continue
            sent = self._local(it.occurred_at, tz).date()
            # Upper bound as well as lower: a message cannot be evidence about a day
            # before it was written, and a mis-stamped future date would otherwise give
            # the model a "sent" anchor later than today to resolve "tomorrow" against.
            if not (cutoff <= sent <= today):
                continue
            body = (it.raw or {}).get("message") or it.body_text or ""
            body = re.sub(r"\s+", " ", str(body)).strip()
            if not body and not it.title:
                continue
            pool.append({
                "id": it.external_id,
                "sent": str(sent),
                "sent_weekday": self._local(it.occurred_at, tz).strftime("%A"),
                "subject": it.title or "",
                "body": body[:MAX_BODY_CHARS],
                "tags": [str(t) for t in (it.audience_tags or [])][:4],
            })
        pool.sort(key=lambda p: p["sent"], reverse=True)
        return pool[:MAX_LLM_ITEMS]

    def _llm_user_message(self, pool: list[dict], today: date, subject: dict,
                          child_class: str) -> str:
        head = (f"TODAY is {today} ({today.strftime('%A')}).\n"
                f"The child is {subject['name']}"
                + (f", in {child_class}" if child_class else "") + ".\n"
                f"Only report reminders dated {today} or later.\n\n"
                "MESSAGES (each with the date it was SENT — resolve relative dates "
                "against that date, not against today):\n")
        blocks = []
        for p in pool:
            tags = f" [to: {', '.join(p['tags'])}]" if p["tags"] else ""
            blocks.append(f"---\nid: {p['id']}\nsent: {p['sent']} ({p['sent_weekday']})"
                          f"{tags}\nsubject: {p['subject']}\n{p['body']}")
        return head + "\n".join(blocks)

    @staticmethod
    def _cache_key(pool: list[dict], child_class: str) -> str:
        """Keyed on the messages themselves — deliberately NOT on today's date.

        The reading is a set of ABSOLUTE dates, and `_verify` re-applies the "not in the
        past" test on every build, so yesterday's reading is still correct today. Putting
        the date in the key would expire a perfectly good reading at midnight and make
        the first page view of every day wait on a fresh LLM call for nothing.

        It still refreshes when it should: a new message changes the hash, and so does an
        old one ageing out of the lookback window.
        """
        blob = json.dumps([[p["id"], p["sent"], p["body"][:200]] for p in pool],
                          sort_keys=True)
        h = hashlib.sha256(f"{child_class}|{blob}".encode("utf-8")).hexdigest()[:16]
        return f"reminders:weduc:{h}"

    def _verify(self, proposed: list, pool: list[dict], today: date,
                classes: dict[str, set[str]]) -> tuple[list[dict], int, list[dict]]:
        """Keep only reminders that cite a real message, quote it, and are still ahead.

        This is the same posture as documents.py's quote check: the model proposes, and
        nothing reaches the page on its word alone. Each test below has a failure it
        exists to prevent — a hallucinated id, a paraphrase presented as a quote, a
        stale reminder resurrected, or another class's instruction shown as his.
        """
        by_id = {p["id"]: p for p in pool}
        kept: list[dict] = []
        unverified: list[dict] = []

        def reject(entry: dict, why: str) -> None:
            unverified.append({**entry, "_rejected": why})

        for e in proposed:
            if not isinstance(e, dict):
                continue
            src = by_id.get(str(e.get("source_id") or ""))
            if src is None:
                reject(e, "cites a message that was not in the input")
                continue
            when = self._loose_date(str(e.get("date") or ""), today)
            if when is None:
                reject(e, "no usable date")
                continue
            if when < today:
                reject(e, f"dated {when}, already past")
                continue
            quote = " ".join(str(e.get("quote") or "").lower().split())
            haystack = " ".join(f"{src['subject']} {src['body']}".lower().split())
            if len(quote) < 8 or quote not in haystack:
                reject(e, "quote is not present verbatim in the cited message")
                continue
            for_class = str(e.get("for_class") or "").strip()
            if for_class and self._relevance(for_class, classes) == "other":
                reject(e, f"addressed to {for_class}, not his class")
                continue
            kind = str(e.get("kind") or "note").lower()
            kept.append(self._rem(
                str(when), str(e.get("title") or "").strip()[:80],
                str(e.get("detail") or "").strip()[:200],
                kind if kind in LLM_KINDS else "note",
                source={"kind": "message", "id": src["id"], "title": src["subject"],
                        "sent": src["sent"], "quote": str(e.get("quote") or "")[:180]},
                certainty=("stated" if str(e.get("certainty")) == "stated" else "inferred"),
                scope="mine" if for_class else "school"))
        return kept, len(unverified), unverified

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _rem(iso_date: str, title: str, detail: str, kind: str, *,
             source: Optional[dict] = None, at: str = "", scope: str = "school",
             certainty: str = "stated", alert: bool = False,
             book_by: str = "", badge: Optional[dict] = None) -> dict:
        # `alert` = something is MISSING and can still be fixed (no lunch ordered, a club
        # place still bookable). It is the one thing on the page you might have to act on
        # today, so it is carried as its own flag rather than inferred from wording.
        # `badge` is a status pill the UI shows beside the title: {"text", "tone"} with
        # tone ok|warn|danger. It carries a STATE (booked, book by Sunday) rather than
        # repeating it as prose, so a day of clubs reads as a row of pills at a glance.
        return {"date": iso_date, "title": title, "detail": detail,
                "kind": kind if kind in KINDS else "note", "at": at, "scope": scope,
                "certainty": certainty, "alert": bool(alert), "book_by": book_by,
                "badge": badge or None, "source": source or {}}

    @staticmethod
    def _dedupe(rems: list[dict]) -> list[dict]:
        """One row per thing, even when two sources describe it.

        The calendar feed carries some events twice — an all-day row and a timed row for
        the same title. The all-day one is the truthful one ("Term 1 Starts" arrived
        alongside a stray 08:45 row, which rendered a whole term's start as a quarter-to-ten
        appointment), so when a duplicate pair differs only by having a clock time, the
        untimed one wins.
        """
        best: dict[str, dict] = {}
        order: list[str] = []
        for r in rems:
            k = re.sub(r"[^a-z0-9]+", "", f"{r['date']}{r['title']}".lower())[:48]
            if k not in best:
                best[k] = r
                order.append(k)
            elif best[k]["at"] and not r["at"]:
                best[k] = r
        return [best[k] for k in order]

    @staticmethod
    def _local(dt: datetime, tz) -> datetime:
        # Portal timestamps are UTC (verified against the recurring swimming slot, which
        # lands on 12:40 local either side of the BST changeover). A naive one is treated
        # as UTC rather than as local, so a missing suffix can't silently shift an hour.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz)

    @staticmethod
    def _loose_date(s: str, today: date) -> Optional[date]:
        """Find a date inside the strings a model, a newsletter or a filename writes.

        Handles ISO, "17 July 2026", "17th July 2026" (how the newsletters are titled)
        and "July 17, 2026". Vaguer phrases ("over the summer holidays") return None
        deliberately — an undated action belongs in the standing list, not pinned to a
        day we invented for it.
        """
        s = " ".join(str(s or "").split())
        if not s:
            return None
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
        if m:
            try:
                return date(int(m[1]), int(m[2]), int(m[3]))
            except ValueError:
                return None
        # strip ordinal suffixes so "17th July" parses like "17 July"
        flat = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", s, flags=re.I).replace(",", "")
        for pat, order in ((r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})\b", "dmy"),
                           (r"\b([A-Za-z]{3,9})\s+(\d{1,2})\s+(\d{4})\b", "mdy")):
            m = re.search(pat, flat)
            if not m:
                continue
            day, mon, yr = ((m[1], m[2], m[3]) if order == "dmy" else (m[2], m[1], m[3]))
            for fmt in ("%d %B %Y", "%d %b %Y"):
                try:
                    return datetime.strptime(f"{day} {mon} {yr}", fmt).date()
                except ValueError:
                    continue
        m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", flat)
        if m:
            try:
                return date(int(m[3]), int(m[2]), int(m[1]))     # UK order
            except ValueError:
                return None
        return None

    def _data_quality(self, state: dict, now: datetime) -> dict:
        dq = dict(state.get("data_quality") or {})
        collected = dq.get("collected_at")
        if collected:
            try:
                age = (now - self._local(datetime.fromisoformat(collected), now.tzinfo))
                dq["stale_hours"] = round(age.total_seconds() / 3600, 1)
            except Exception:
                pass
        dq.setdefault("live", False)
        dq.setdefault("note", "no Weduc data collected yet — run the source")
        return dq
