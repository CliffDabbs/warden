"""Weduc / ReachMoreParents adapter — the school-portal source.

Fixtures-first: the live portal is a JS SPA behind a form login (recon in
docs/weduclukedigestHANDOVER (2).md). Endpoint mapping is still open, so v1 ships the
recon capture in fixtures/snapshot.json and serves that. If a run is forced live with
creds we note it and still use fixtures — no scrape/side-effects (read-only, v1).

Mechanical only (contract §2): fetch → normalise → Item[], with faithful `audience_tags`
("Jellyfish Class", "whole-school") and `subject_ids` where the source names the child.
Relevance/summarisation are the hub's job, not this adapter's. One derived signal:
  * weduc.<subject>.forms_outstanding (number) — count of outstanding forms.

Existence of this adapter is the reusability proof: a second source dropped in with zero
core changes (contract §8 step 5).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ...models import Attachment, Item, Signal, SignalType, utcnow
from ..base import (
    AdapterManifest, CollectResult, HealthResult, SourceAdapter, SourceContext,
)

_MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.json"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


class Adapter(SourceAdapter):
    manifest: AdapterManifest = AdapterManifest.model_validate_json(
        _MANIFEST_PATH.read_text(encoding="utf-8")
    )

    async def authenticate(self, ctx: SourceContext) -> dict[str, Any]:
        # Recon-only in v1: no live scraper wired yet. A forced-live run is honoured by
        # noting it, then falling back to the captured fixtures (nothing is submitted).
        warning = ""
        if ctx.live and ctx.has_secrets:
            warning = "live weduc not implemented (recon-only); used fixtures"
        return {"mode": "fixtures", "warning": warning}

    async def collect(self, ctx: SourceContext, session: dict[str, Any]) -> CollectResult:
        subject = ctx.subject or "luke"
        warning = session.get("warning", "")
        data: dict[str, Any] = {}
        p = ctx.fixtures_path()
        if p and (p / "snapshot.json").exists():
            data = _read_json(p / "snapshot.json")

        child_class = (data.get("child") or {}).get("class")
        items: list[Item] = []

        # ── newsfeed posts ──────────────────────────────────────────────────
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

        # ── forms (drives the outstanding-forms signal) ─────────────────────
        outstanding: list[str] = []
        for form in data.get("forms", []):
            is_out = form.get("status") == "outstanding"
            if is_out:
                outstanding.append(form.get("title", ""))
            items.append(Item(
                source_id="weduc",
                subject_ids=[subject],                 # forms are per-child in the portal
                external_id=f"weduc:form:{form['id']}",
                kind="form",
                title=form.get("title", ""),
                body_text=f"Status: {form.get('status', '')}",
                due_at=_parse_dt(form.get("due_at")),
                audience_tags=form.get("audience_tags", []),
                url=form.get("url"),
                raw=form,
            ))

        # ── calendar events ─────────────────────────────────────────────────
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

        signal = Signal(
            key=f"weduc.{subject}.forms_outstanding",
            value=len(outstanding),
            type=SignalType.number,
            source="weduc", subject=subject,
            meta={"forms": outstanding},
        )
        detail = f"fixtures: {len(items)} items, {len(outstanding)} forms outstanding"
        if warning:
            detail += f" — {warning}"
        return CollectResult(
            ok=True, items=items, signals=[signal],
            next_cursor=data.get("captured_at") or utcnow().isoformat(), detail=detail,
        )

    async def healthcheck(self, ctx: SourceContext) -> HealthResult:
        p = ctx.fixtures_path()
        ok = bool(p and (p / "snapshot.json").exists())
        return HealthResult(ok=ok, detail="fixtures present" if ok else "snapshot.json missing")
