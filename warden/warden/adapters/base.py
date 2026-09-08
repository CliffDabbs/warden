"""Source-adapter contract — the pluggable-scraper interface.

A source is anything Warden can log into and read: a learning platform, a school
portal, (future) a WhatsApp export, an RSS feed. Each adapter implements this small
surface; the core knows nothing source-specific (see docs/sourceadapterCONTRACT.md).

Two responsibilities, both mechanical:
  1. fetch + normalise  -> Item[]   (faithful, complete, deduped by external_id)
  2. derive signals     -> Signal[] (typed facts the rules engine reacts to)

Signal derivation lives in the adapter because only the adapter understands its own
source's shape (e.g. what "daily learning complete" looks like on Atom). It is still
mechanical extraction, not relevance reasoning — the rules engine owns decisions.

Every adapter MUST run in `fixture mode`: when no secrets/browser are available it
loads saved captures from its fixtures/ dir and still returns Items + Signals, so the
whole app is demoable offline. `SourceContext.live` tells the adapter which mode it is in.
"""
from __future__ import annotations

import abc
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..models import Item, Signal


class AdapterManifest(BaseModel):
    id: str
    display_name: str
    version: str = "1.0.0"
    auth_type: str = "form_login_session"     # form_login_session | api_key | none
    capabilities: list[str] = Field(default_factory=list)
    secrets_required: list[str] = Field(default_factory=list)
    emits_signals: list[str] = Field(default_factory=list)   # signal keys this adapter can emit


class SourceContext(BaseModel):
    """Everything an adapter run needs. Assembled by the registry per run."""
    source_key: str
    subject: Optional[str] = None
    secrets: dict[str, str] = Field(default_factory=dict)     # resolved values (may be empty)
    options: dict[str, Any] = Field(default_factory=dict)
    since_cursor: Optional[str] = None
    fixtures_dir: Optional[str] = None
    live: bool = False                       # True => real login/scrape; False => fixtures only

    @property
    def has_secrets(self) -> bool:
        return bool(self.secrets) and all(self.secrets.values())

    def fixtures_path(self) -> Optional[Path]:
        return Path(self.fixtures_dir) if self.fixtures_dir else None


class CollectResult(BaseModel):
    ok: bool = True
    # Did this data actually come from the live source? Fixtures are for demoing with no
    # credentials — they must never be mistaken for real data by the rules engine, which
    # flips real network switches. An adapter asked for a LIVE run that fails must report
    # ok=False rather than quietly substituting the sample.
    live: bool = False
    items: list[Item] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    # A structured, LLM-friendly "state of play" snapshot for the dynamic rule
    # evaluator to reason over (e.g. Atom: today's minutes, topics, assignments).
    # Stored latest-per-source by the registry; distinct from discrete signals.
    state: dict[str, Any] = Field(default_factory=dict)
    next_cursor: Optional[str] = None
    detail: str = ""


class HealthResult(BaseModel):
    ok: bool = True
    detail: str = ""


class SourceAdapter(abc.ABC):
    """Implement this per source. Keep it stateless; the hub owns cursors/state."""

    #: static metadata; subclasses set this
    manifest: AdapterManifest

    @abc.abstractmethod
    async def authenticate(self, ctx: SourceContext) -> dict[str, Any]:
        """Establish a session; return a persistable/refreshable blob (may be {})."""

    @abc.abstractmethod
    async def collect(self, ctx: SourceContext, session: dict[str, Any]) -> CollectResult:
        """Fetch since ctx.since_cursor, normalise to Item[], derive Signal[]."""

    @abc.abstractmethod
    async def healthcheck(self, ctx: SourceContext) -> HealthResult:
        """Cheap liveness/structure check."""
